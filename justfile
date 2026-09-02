# Task runner for gcmon. Every recipe carries `poetry run`, so a recipe is
# correct from any shell; `just --list` shows what is available.
#
# `sh` is not on PATH in PowerShell on Windows, and `bash` there may be WSL,
# so recipes run through Windows PowerShell instead. Keep every recipe a single
# command with no shell-specific syntax, so it works under both shells.
set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

# Show the available recipes
default:
    @just --list

# Install every dependency group and extra
install:
    poetry install --all-groups --all-extras

# Run the test suite
test:
    poetry run pytest -q --basetemp=.temp

# Run the test suite with coverage and a JUnit report
coverage:
    poetry run pytest -q --basetemp=.temp --cov=src/gcmon --cov-report=xml:coverage.xml --cov-report=term-missing --cov-branch --junitxml=reports/tests.xml

# Check the layer boundaries
architecture:
    poetry run pytest -m architecture

# Run every stress test
stress: stress-control stress-threads

# Repeat the control-plane tests
stress-control:
    poetry run pytest --basetemp=.temp -k "control" -s --count 40

# Repeat the thread-safety stress tests
stress-threads:
    poetry run pytest --basetemp=.temp -m stress --count 20

# Run the randomized differential tests
fuzz:
    poetry run pytest --basetemp=.temp -m fuzz

# Run the CodSpeed benchmarks
bench:
    poetry run pytest tests/benchmarks -m "benchmark" --codspeed

# Lint with ruff
lint:
    poetry run ruff check src

# Run both type checkers
typecheck: typecheck-mypy typecheck-pyrefly

# Type check with mypy
typecheck-mypy:
    poetry run mypy src tests

# Type check with pyrefly
typecheck-pyrefly:
    poetry run pyrefly check src tests

# Build the distribution
build:
    poetry build

# Validate the built distribution
check-dist:
    poetry run twine check dist/*

# Preview the changelog section for a tag, or for the pyproject version
changelog tag="":
    python .github/scripts/extract_changelog.py {{tag}}

# Run every pre-commit hook over the tree
precommit:
    poetry run pre-commit run --all-files

# Rewrap the named Markdown files at 78 columns
wrap +paths:
    python .github/scripts/wrap_markdown.py {{paths}}
