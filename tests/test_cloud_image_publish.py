from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
WORKFLOW = (ROOT / ".github" / "workflows" / "cloud-publish.yml").read_text()
PARAMS = (ROOT / "infra" / "azure" / "main.bicepparam").read_text()
DEPLOY = (ROOT / "infra" / "azure" / "DEPLOY.md").read_text()


def test_publish_workflow_is_main_only_and_has_exact_write_permissions():
    assert re.search(r"(?m)^  push:\n    branches:\n      - main$", WORKFLOW)
    assert re.search(r"(?m)^  workflow_dispatch:$", WORKFLOW)
    assert "pull_request:" not in WORKFLOW
    assert "if: github.ref == 'refs/heads/main'" in WORKFLOW
    assert re.search(r"(?m)^  packages: write$", WORKFLOW)
    assert re.search(r"(?m)^  id-token: write$", WORKFLOW)
    assert re.search(r"(?m)^  contents: read$", WORKFLOW)
    permissions = WORKFLOW.split("permissions:\n", 1)[1].split("\nenv:\n", 1)[0]
    assert set(re.findall(r"(?m)^  ([a-z-]+):", permissions)) == {
        "packages",
        "id-token",
        "contents",
    }
    assert "runs-on: ubuntu-latest" in WORKFLOW


def test_publish_workflow_builds_one_amd64_image_and_verifies_its_signature():
    assert WORKFLOW.count("docker/build-push-action@v6") == 1
    assert "platforms: linux/amd64" in WORKFLOW
    assert "push: true" in WORKFLOW
    assert "ghcr.io/poopaskoopa/wattracker-cloud:${{ steps.image.outputs.tag }}" in WORKFLOW
    assert 'echo "tag=sha-${COMMIT_SHA:0:7}"' in WORKFLOW
    assert WORKFLOW.count("cosign sign --yes") == 1
    assert WORKFLOW.count("cosign verify") == 1
    assert "secrets.GITHUB_TOKEN" in WORKFLOW
    assert "GITHUB_STEP_SUMMARY" in WORKFLOW
    assert 'image_ref="${IMAGE}@${DIGEST}"' in WORKFLOW
    assert "${{ steps.build.outputs.digest }}" in WORKFLOW
    assert 'Published commit: ${GITHUB_SHA}' in WORKFLOW


def test_deployment_skeleton_and_runbook_use_one_signed_digest_for_both_planes():
    assert "successful #316 `.github/workflows/cloud-publish.yml` run" in PARAMS
    assert "same successful #316 workflow run and digest as readImage" in PARAMS
    assert "param readImage = 'TODO_SIGNED_IMMUTABLE_READ_IMAGE_FROM_217_OUTPUT'" in PARAMS
    assert "param syncImage = 'TODO_SIGNED_IMMUTABLE_SYNC_IMAGE_FROM_217_OUTPUT'" in PARAMS
    assert ".github/workflows/cloud-publish.yml" in DEPLOY
    assert "ghcr.io/poopaskoopa/wattracker-cloud@sha256:" in DEPLOY
    assert "cosign verify" in DEPLOY
    assert "same full image reference" in DEPLOY
    assert "check_cloud_image_drift.py" in DEPLOY
    assert "--deployment-commit" in DEPLOY
    assert "The package is **public** and needs no registry pull credential" in DEPLOY
    assert "inherits that repository's\nvisibility" in DEPLOY
    assert "OCI image index, not a single manifest" in DEPLOY
    assert "provenance: false" in DEPLOY
