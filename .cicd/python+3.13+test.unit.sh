#!/bin/sh
# The unit suite. The filename picks the image (docker.io/library/python:3.13)
# and the phase, so there is nothing to configure elsewhere.
#
# SimpiCI mounts the checkout read-only at CICD_WORKSPACE, and an editable
# install writes .egg-info next to pyproject.toml, so the tree is copied to a
# writable path first. Installing from pyproject rather than naming the
# dependencies here keeps this script from drifting away from the package.
set -eu

cp -a "$CICD_WORKSPACE" /tmp/src
cd /tmp/src

pip install --quiet --disable-pip-version-check --root-user-action=ignore -e ".[dev]"
exec pytest -q
