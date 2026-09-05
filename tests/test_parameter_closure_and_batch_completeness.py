from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mycode.evidence.image_batch import build_image_understanding_batch
from mycode.flow_analysis.parameter_closure import trace_parameter_closures
from mycode.repo_index.structure_index import RepositoryIndex
from mycode.schemas.evidence import NormalizedSample


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _write_tiny_png(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(path, format="PNG")


def test_parameter_closure_links_public_api_backend_and_serializer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo_root = Path(tmp)
        _write(
            repo_root / "src/cryptography/hazmat/primitives/_serialization.py",
            "\n".join(
                [
                    "class BestAvailableEncryption:",
                    "    def __init__(self, password, kdf_rounds=None):",
                    "        self.password = password",
                    "        self.kdf_rounds = kdf_rounds",
                    "",
                    "class _KeySerializationEncryption:",
                    "    def __init__(self, password, kdf_rounds=None):",
                    "        self.password = password",
                    "        self.kdf_rounds = kdf_rounds",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/backends/openssl/backend.py",
            "\n".join(
                [
                    "from cryptography.hazmat.primitives.serialization import ssh",
                    "",
                    "def _private_key_bytes(key, encoding, format, encryption_algorithm):",
                    "    if format == 'OpenSSH':",
                    "        return ssh._serialize_ssh_private_key(",
                    "            key, encryption_algorithm.password, encryption_algorithm.kdf_rounds",
                    "        )",
                    "    return b''",
                ]
            ),
        )
        _write(
            repo_root / "src/cryptography/hazmat/primitives/serialization/ssh.py",
            "\n".join(
                [
                    "def _serialize_ssh_private_key(key, password, kdf_rounds=None):",
                    "    rounds = kdf_rounds or 16",
                    "    return b'openssh' + str(rounds).encode()",
                ]
            ),
        )
        index = RepositoryIndex(
            repo="pyca/cryptography",
            instance_id="pyca__cryptography-7520",
            repo_root=repo_root,
            structure_path=repo_root / "missing.json",
        )
        flows = trace_parameter_closures(
            index,
            issue_text="BestAvailableEncryption should pass kdf_rounds through backend OpenSSH serializer.",
            queries=["kdf_rounds", "_private_key_bytes", "_serialize_ssh_private_key"],
            limit=5,
        )
        kdf = next(flow for flow in flows if flow["term"].lower() == "kdf_rounds")
        paths = {item["path"] for item in kdf["locations"]}
        assert kdf["flow_type"] == "serializer_backend_call_chain"
        assert "src/cryptography/hazmat/primitives/_serialization.py" in paths
        assert "src/cryptography/hazmat/backends/openssl/backend.py" in paths
        assert "src/cryptography/hazmat/primitives/serialization/ssh.py" in paths
        assert any(edge["relation"] in {"passes_term_to_called_symbol_or_neighbor", "imports_or_reexports_target_module"} for edge in kdf["edges"])


def test_image_batch_completeness_counts_only_processable_images() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        png = root / "assets" / "one.png"
        svg = root / "assets" / "logo.svg"
        _write_tiny_png(png)
        _write(svg, "<svg xmlns='http://www.w3.org/2000/svg'></svg>")
        old_image_dir = os.environ.get("MYCODE_IMAGE_DIR")
        os.environ["MYCODE_IMAGE_DIR"] = str(root / "assets")
        sample = NormalizedSample(
            instance_id="demo__repo-1",
            repo="demo/repo",
            dataset="unit",
            issue_text=(
                "Actual chart screenshot: https://example.test/one.png\n"
                "Logo asset: https://example.test/logo.svg"
            ),
            raw={},
            gold_files=["src/plugin.js"],
        )
        try:
            summary = build_image_understanding_batch(
                [sample],
                cache_path=root / "image_batch.jsonl",
                asset_cache_dir=root / "cache",
                allow_network=False,
                use_vlm=False,
                reuse_cache=True,
            )
        finally:
            if old_image_dir is None:
                os.environ.pop("MYCODE_IMAGE_DIR", None)
            else:
                os.environ["MYCODE_IMAGE_DIR"] = old_image_dir
        assert summary["expected_images"] == 2
        assert summary["processable_images"] == 1
        assert summary["covered_processable_images"] == 1
        assert summary["non_processable_images"] == 1
        assert summary["complete_for_processable_images"] is True
        assert Path(summary["manifest_path"]).exists()


if __name__ == "__main__":
    test_parameter_closure_links_public_api_backend_and_serializer()
    test_image_batch_completeness_counts_only_processable_images()
