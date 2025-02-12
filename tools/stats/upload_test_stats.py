import argparse
import os
import requests
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Any, Tuple
from tempfile import TemporaryDirectory

import rockset  # type: ignore[import]
import boto3  # type: ignore[import]

PYTORCH_REPO = "https://api.github.com/repos/pytorch/pytorch"
S3_RESOURCE = boto3.resource("s3")


def get_request_headers() -> Dict[str, str]:
    return {
        "Accept": "application/vnd.github.v3+json",
        "Authorization": "token " + os.environ["GITHUB_TOKEN"],
    }


def parse_xml_report(
    tag: str, report: Path, workflow_id: int, workflow_run_attempt: int
) -> List[Dict[str, Any]]:
    """Convert a test report xml file into a JSON-serializable list of test cases."""
    print(f"Parsing {tag}s for test report: {report}")
    # [Job id in artifacts]
    # Retrieve the job id from the report path. In our GHA workflows, we append
    # the job id to the end of the report name, so `report` looks like:
    #     unzipped-test-reports-foo_5596745227/test/test-reports/foo/TEST-foo.xml
    # and we want to get `5596745227` out of it.
    job_id = int(report.parts[0].rpartition("_")[2])
    print(f"Found job id: {job_id}")

    root = ET.parse(report)

    test_cases = []
    for test_case in root.iter(tag):
        case = process_xml_element(test_case)
        case["workflow_id"] = workflow_id
        case["workflow_run_attempt"] = workflow_run_attempt
        case["job_id"] = job_id
        test_cases.append(case)

    return test_cases


def process_xml_element(element: ET.Element) -> Dict[str, Any]:
    """Convert a test suite element into a JSON-serializable dict."""
    ret: Dict[str, Any] = {}

    # Convert attributes directly into dict elements.
    # e.g.
    #     <testcase name="test_foo" classname="test_bar"></testcase>
    # becomes:
    #     {"name": "test_foo", "classname": "test_bar"}
    ret.update(element.attrib)

    # The XML format encodes all values as strings. Convert to ints/floats if
    # possible to make aggregation possible in Rockset.
    for k, v in ret.items():
        try:
            ret[k] = int(v)
        except ValueError:
            pass
        try:
            ret[k] = float(v)
        except ValueError:
            pass

    # Convert inner and outer text into special dict elements.
    # e.g.
    #     <testcase>my_inner_text</testcase> my_tail
    # becomes:
    #     {"text": "my_inner_text", "tail": " my_tail"}
    if element.text and element.text.strip():
        ret["text"] = element.text
    if element.tail and element.tail.strip():
        ret["tail"] = element.tail

    # Convert child elements recursively, placing them at a key:
    # e.g.
    #     <testcase>
    #       <foo>hello</foo>
    #     </testcase>
    # becomes
    #    {"foo": {"text": "hello"}}
    for child in element:
        ret[child.tag] = process_xml_element(child)
    return ret


def get_artifact_urls(workflow_run_id: int) -> Dict[Path, str]:
    """Get all workflow artifacts with 'test-report' in the name."""
    response = requests.get(
        f"{PYTORCH_REPO}/actions/runs/{workflow_run_id}/artifacts?per_page=100",
    )
    artifacts = response.json()["artifacts"]
    while "next" in response.links.keys():
        response = requests.get(
            response.links["next"]["url"], headers=get_request_headers()
        )
        artifacts.extend(response.json()["artifacts"])

    artifact_urls = {}
    for artifact in artifacts:
        if "test-report" in artifact["name"]:
            artifact_urls[Path(artifact["name"])] = artifact["archive_download_url"]
    return artifact_urls


def unzip(p: Path) -> None:
    """Unzip the provided zipfile to a similarly-named directory.

    Returns None if `p` is not a zipfile.

    Looks like: /tmp/test-reports.zip -> /tmp/unzipped-test-reports/
    """
    assert p.is_file()
    unzipped_dir = p.with_name("unzipped-" + p.stem)

    with zipfile.ZipFile(p, "r") as zip:
        zip.extractall(unzipped_dir)


def download_and_extract_artifact(
    artifact_name: Path, artifact_url: str, workflow_run_attempt: int
) -> None:
    # [Artifact run attempt]
    # All artifacts on a workflow share a single namespace. However, we can
    # re-run a workflow and produce a new set of artifacts. To avoid name
    # collisions, we add `-runattempt1<run #>-` somewhere in the artifact name.
    #
    # This code parses out the run attempt number from the artifact name. If it
    # doesn't match the one specified on the command line, skip it.
    atoms = str(artifact_name).split("-")
    for atom in atoms:
        if atom.startswith("runattempt"):
            found_run_attempt = int(atom[len("runattempt") :])
            if workflow_run_attempt != found_run_attempt:
                print(
                    f"Skipping {artifact_name} as it is an invalid run attempt. "
                    f"Expected {workflow_run_attempt}, found {found_run_attempt}."
                )

    print(f"Downloading and extracting {artifact_name}")

    response = requests.get(artifact_url, headers=get_request_headers())
    with open(artifact_name, "wb") as f:
        f.write(response.content)
    unzip(artifact_name)


def download_and_extract_s3_reports(
    workflow_run_id: int, workflow_run_attempt: int
) -> None:
    bucket = S3_RESOURCE.Bucket("gha-artifacts")
    objs = bucket.objects.filter(
        Prefix=f"pytorch/pytorch/{workflow_run_id}/{workflow_run_attempt}/artifact/test-reports"
    )

    found_one = False
    for obj in objs:
        found_one = True
        p = Path(Path(obj.key).name)
        print(f"Downloading and extracting {p}")
        with open(p, "wb") as f:
            f.write(obj.get()["Body"].read())
        unzip(p)

    if not found_one:
        raise RuntimeError(
            "Didn't find any test reports in s3, there is probably a bug!"
        )


def download_and_extract_gha_artifacts(
    workflow_run_id: int, workflow_run_attempt: int
) -> None:
    artifact_urls = get_artifact_urls(workflow_run_id)
    for name, url in artifact_urls.items():
        download_and_extract_artifact(Path(name), url, workflow_run_attempt)


def upload_to_rockset(collection: str, docs: List[Any]) -> None:
    print(f"Writing {len(docs)} documents to Rockset")
    client = rockset.Client(
        api_server="api.rs2.usw2.rockset.com", api_key=os.environ["ROCKSET_API_KEY"]
    )
    client.Collection.retrieve(collection).add_docs(docs)
    print("Done!")


def get_tests(
    workflow_run_id: int, workflow_run_attempt: int
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    with TemporaryDirectory() as temp_dir:
        print("Using temporary directory:", temp_dir)
        os.chdir(temp_dir)

        # Download and extract all the reports (both GHA and S3)
        download_and_extract_s3_reports(workflow_run_id, workflow_run_attempt)
        download_and_extract_gha_artifacts(workflow_run_id, workflow_run_attempt)

        # Parse the reports and transform them to JSON
        test_cases = []
        test_suites = []
        for xml_report in Path(".").glob("**/*.xml"):
            test_cases.extend(
                parse_xml_report(
                    "testcase",
                    xml_report,
                    workflow_run_id,
                    workflow_run_attempt,
                )
            )
            test_suites.extend(
                parse_xml_report(
                    "testsuite",
                    xml_report,
                    workflow_run_id,
                    workflow_run_attempt,
                )
            )

        return test_cases, test_suites


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload test stats to Rockset")
    parser.add_argument(
        "--workflow-run-id",
        type=int,
        required=True,
        help="id of the workflow to get artifacts from",
    )
    parser.add_argument(
        "--workflow-run-attempt",
        type=int,
        required=True,
        help="which retry of the workflow this is",
    )
    args = parser.parse_args()
    test_cases, test_suites = get_tests(args.workflow_run_id, args.workflow_run_attempt)
    upload_to_rockset("test_run", test_cases)
    upload_to_rockset("test_suite", test_suites)
