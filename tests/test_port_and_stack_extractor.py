import json

from tools.port_and_stack_extractor import _default_port_from_stack_tokens, _extract_stack_tokens
from tools.port_and_stack_extractor import _extract_ports_from_package_json


def test_default_port_prefers_vite_over_generic_frontend_tokens():
    port, source = _default_port_from_stack_tokens(["node", "react", "vite"])

    assert port == 5173
    assert source == "vite"


def test_default_port_prefers_uvicorn_fastapi_backend_defaults():
    port, source = _default_port_from_stack_tokens(["python", "fastapi", "uvicorn"])

    assert port == 8000
    assert source == "uvicorn"


def test_default_port_prefers_streamlit_when_present():
    port, source = _default_port_from_stack_tokens(["python", "streamlit"])

    assert port == 8501
    assert source == "streamlit"


def test_default_port_prefers_astro_over_generic_node_defaults():
    port, source = _default_port_from_stack_tokens(["node", "react", "astro"])

    assert port == 4321
    assert source == "astro"


def test_extract_stack_tokens_includes_vite_dependency(tmp_path):
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps(
            {
                "dependencies": {
                    "react": "18.2.0"
                },
                "devDependencies": {
                    "vite": "5.4.0"
                },
            }
        ),
        encoding="utf-8",
    )

    tokens = _extract_stack_tokens(str(tmp_path))

    assert "vite" in tokens
    assert "react" in tokens
    assert "node" in tokens


def test_extract_stack_tokens_detects_bun_package_manager(tmp_path):
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps(
            {
                "name": "bun-app",
                "packageManager": "bun@1.1.0"
            }
        ),
        encoding="utf-8",
    )

    tokens = _extract_stack_tokens(str(tmp_path))

    assert "bun" in tokens
    assert "node" in tokens


def test_extract_stack_tokens_detects_streamlit_from_pyproject(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        """
[project]
dependencies = [
  \"streamlit>=1.38\",
  \"pandas>=2.2\"
]
""".strip(),
        encoding="utf-8",
    )

    tokens = _extract_stack_tokens(str(tmp_path))

    assert "python" in tokens
    assert "streamlit" in tokens


def test_extract_stack_tokens_respects_build_context(tmp_path):
    root_package = tmp_path / "package.json"
    root_package.write_text(
        json.dumps(
            {
                "name": "root",
                "dependencies": {
                    "next": "14.0.0"
                },
            }
        ),
        encoding="utf-8",
    )

    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    backend_requirements = backend_dir / "requirements.txt"
    backend_requirements.write_text("fastapi==0.110.0\nuvicorn==0.29.0\n", encoding="utf-8")

    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    frontend_package = frontend_dir / "package.json"
    frontend_package.write_text(
        json.dumps(
            {
                "name": "frontend",
                "dependencies": {
                    "react": "18.2.0"
                },
                "devDependencies": {
                    "vite": "5.4.0"
                },
            }
        ),
        encoding="utf-8",
    )

    backend_tokens = _extract_stack_tokens(str(tmp_path), build_context="backend")
    frontend_tokens = _extract_stack_tokens(str(tmp_path), build_context="frontend")

    assert "fastapi" in backend_tokens
    assert "uvicorn" in backend_tokens
    assert "python" in backend_tokens
    assert "next" not in backend_tokens
    assert "vite" in frontend_tokens
    assert "react" in frontend_tokens
    assert "fastapi" not in frontend_tokens


def test_extract_ports_from_package_json_infers_vite_default_from_dev_script(tmp_path):
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps(
            {
                "name": "vite-app",
                "scripts": {
                    "dev": "vite",
                    "build": "vite build"
                },
                "devDependencies": {
                    "vite": "5.4.0"
                },
            }
        ),
        encoding="utf-8",
    )

    ports = _extract_ports_from_package_json(str(tmp_path), build_context=".")

    assert (5173, 0.68) in ports


def test_extract_stack_tokens_detects_uvicorn_from_dockerfile(tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(
        """
FROM python:3.12-slim
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
""".strip(),
        encoding="utf-8",
    )

    tokens = _extract_stack_tokens(str(tmp_path), build_context=".")

    assert "python" in tokens
    assert "uvicorn" in tokens


def test_extract_stack_tokens_inferrs_uvicorn_for_fastapi_python(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi==0.110.0\n", encoding="utf-8")

    tokens = _extract_stack_tokens(str(tmp_path), build_context=".")

    assert "python" in tokens
    assert "fastapi" in tokens
    assert "uvicorn" in tokens