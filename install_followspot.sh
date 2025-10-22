#!/usr/bin/env bash
# FollowSpot installation helper
#
# This script automates the setup documented in the README. It installs the
# required system dependencies, clones the FollowSpot repository, and prepares
# the Python virtual environment with the project's Python packages.
#
# Usage (non-interactive, recommended):
#   curl -fsSL https://raw.githubusercontent.com/Fly-My-Fixtures/Fly-My-Fixtures/main/install_followspot.sh | bash
#
# You can override defaults by exporting environment variables before piping the
# script to bash, e.g.:
#   REPO_URL=https://github.com/example/Fly-My-Fixtures.git \
#   INSTALL_DIR=$HOME/custom-followspot \
#   curl -fsSL .../install_followspot.sh | bash
#
# The script is designed to be easy to read and modify. Each major step is
# broken into dedicated helper functions for clarity.

set -euo pipefail

# ------------------------------- Configuration -------------------------------
# Default repository URL. Override with REPO_URL env var if you are working from
# a fork or mirror.
REPO_URL=${REPO_URL:-"https://github.com/Fly-My-Fixtures/Fly-My-Fixtures.git"}

# Default branch to clone. Override with BRANCH env var if desired.
BRANCH=${BRANCH:-"main"}

# Installation directory (where the repo will live). Override with INSTALL_DIR.
INSTALL_DIR=${INSTALL_DIR:-"$HOME/followspot"}

# Virtual environment directory (inside INSTALL_DIR by default).
VENV_DIR=${VENV_DIR:-"$INSTALL_DIR/.venv"}

# System packages required by FollowSpot.
APT_PACKAGES=(
  python3
  python3-pip
  python3-venv
  libsdl2-2.0-0
  python3-gpiozero
  python3-lgpio
  git
)

# Python packages required inside the virtual environment.
PYTHON_PACKAGES=(
  flask
  pygame
  sacn
)

# ------------------------------- Helper Output ------------------------------
log() {
  printf '\n%s\n' "==> $*"
}

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

# Determine whether sudo is available / necessary.
detect_sudo() {
  if command -v sudo >/dev/null 2>&1 && [ "${EUID}" -ne 0 ]; then
    SUDO="sudo"
  else
    SUDO=""
  fi
}

# Print a short summary when the user requests help.
print_help() {
  cat <<'USAGE'
FollowSpot automated installer

Environment overrides:
  REPO_URL    - Git repository to clone (default: official Fly-My-Fixtures repo)
  BRANCH      - Branch or tag to checkout (default: main)
  INSTALL_DIR - Target directory for the clone (default: ~/followspot)
  VENV_DIR    - Virtual environment path (default: $INSTALL_DIR/.venv)

Example:
  INSTALL_DIR=$HOME/followspot-prod \
  BRANCH=stable \
  bash install_followspot.sh
USAGE
}

# ----------------------------- Pre-flight checks ----------------------------
check_prerequisites() {
  log "Checking required commands"

  command -v apt-get >/dev/null 2>&1 || die "apt-get is required but was not found (Debian/Ubuntu only)"
}

# --------------------------- System package install -------------------------
install_system_packages() {
  log "Installing system packages"

  detect_sudo

  # Refresh the package index.
  $SUDO apt-get update

  # Install dependencies in a non-interactive fashion.
  DEBIAN_FRONTEND=noninteractive \
    $SUDO apt-get install -y "${APT_PACKAGES[@]}"
}

# ------------------------------- Git handling -------------------------------
clone_or_update_repo() {
  log "Preparing installation directory at $INSTALL_DIR"

  command -v git >/dev/null 2>&1 || die "git was not found after installation"

  mkdir -p "$(dirname "$INSTALL_DIR")"

  if [ -d "$INSTALL_DIR/.git" ]; then
    log "Existing repository detected – pulling latest changes"
    git -C "$INSTALL_DIR" fetch --all --tags
    git -C "$INSTALL_DIR" checkout "$BRANCH"
    git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"
  else
    if [ -d "$INSTALL_DIR" ] && [ "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
      die "Install directory $INSTALL_DIR exists and is not empty"
    fi
    log "Cloning $REPO_URL (branch: $BRANCH)"
    git clone --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
  fi
}

# ----------------------------- Virtual environment --------------------------
create_virtualenv() {
  log "Setting up Python virtual environment at $VENV_DIR"

  command -v python3 >/dev/null 2>&1 || die "python3 was not found after installation"

  if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv "$VENV_DIR"
  else
    log "Virtual environment already exists – reusing"
  fi
}

install_python_packages() {
  log "Installing Python packages into the virtual environment"

  local python_bin="$VENV_DIR/bin/python"
  local pip_bin="$VENV_DIR/bin/pip"

  [ -x "$python_bin" ] || die "Expected python executable at $python_bin"

  "$python_bin" -m pip install --upgrade pip
  "$pip_bin" install --upgrade "${PYTHON_PACKAGES[@]}"
}

# ------------------------------- Final summary ------------------------------
print_summary() {
  cat <<SUMMARY

Installation complete! Next steps:
  1. Activate the virtual environment:
       source "$VENV_DIR/bin/activate"
  2. Start the FollowSpot server:
       python main.py
  3. Open your browser to http://<server-ip>:8080/

Additional tips:
  • To start on boot, create a systemd service referencing $INSTALL_DIR.
  • Update later by running:
       cd "$INSTALL_DIR" && git pull
       "$VENV_DIR/bin/pip" install --upgrade ${PYTHON_PACKAGES[*]}
SUMMARY
}

# ---------------------------------- Main ------------------------------------
main() {
  if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ]; then
    print_help
    exit 0
  fi

  check_prerequisites
  install_system_packages
  clone_or_update_repo
  create_virtualenv
  install_python_packages
  print_summary
}

main "$@"
