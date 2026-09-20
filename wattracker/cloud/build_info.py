"""Build identity used by the cloud version endpoint.

The cloud image build replaces this source fallback with the full git commit
SHA supplied as ``WATTRACKER_CLOUD_COMMIT``.
"""

COMMIT = "source"
