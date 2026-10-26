# List the available development commands.
[group("general")]
default:
    @just --list

# Format Rust sources.
[group("format")]
fmt-rust:
    cargo fmt --manifest-path binary-utils/Cargo.toml

# Format Python sources, tests, examples, and scripts.
[group("format")]
fmt-python:
    uv run ruff format src tests examples scripts

# Format both languages.
[group("format")]
fmt: fmt-rust fmt-python

# Check Rust compilation and deny Clippy warnings.
[group("lint")]
lint-rust:
    cargo check --manifest-path binary-utils/Cargo.toml --locked --all-targets
    cargo clippy --manifest-path binary-utils/Cargo.toml --locked --all-targets -- -D warnings

# Check Python lint and formatting without modifying sources.
[group("lint")]
lint-python:
    uv run ruff check src tests examples scripts
    uv run ruff format --check src tests examples scripts

# Lint both languages.
[group("lint")]
lint: lint-rust lint-python

# Run Rust unit tests.
[group("test")]
test-rust:
    cargo test --manifest-path binary-utils/Cargo.toml --locked

# Run Python tests against fresh native artifacts and collect coverage.
[group("test")]
test-python: build-artifacts
    uv run pytest --cov --cov-branch --cov-report=term-missing --cov-report=html:reports/python-html --cov-report=json:reports/python-coverage.json --cov-report=xml:reports/python-coverage.xml

# Run Rust and Python tests.
[group("test")]
test: test-rust test-python

# Build and package all six release binaries and their manifest.
[group("build")]
build-artifacts:
    uv run python scripts/build_artifacts.py

# Delete generated output, preserving .venv.
[group("clean")]
clean:
    rm -rf binary-utils/target artifacts dist reports runs .coverage .coverage.*
