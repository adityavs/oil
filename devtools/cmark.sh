#!/bin/bash
#
# Usage:
#   ./cmark.sh <function name>

set -o nounset
set -o pipefail
set -o errexit

download() {
  wget --directory _tmp \
    https://github.com/commonmark/cmark/archive/0.28.3.tar.gz
}

readonly CMARK_DIR=_tmp/cmark-0.28.3

build() {
  pushd $CMARK_DIR
  # GNU make calls cmake?
  make
  popd

  # Binaries are in build/src
}

run-tests() {
  pushd $CMARK_DIR
  make test
  sudo make install
  popd
}

demo-theirs() {
  echo '*hi*' | cmark
}

demo-ours() {
  echo '*hi*' | devtools/cmark.py
}


"$@"
