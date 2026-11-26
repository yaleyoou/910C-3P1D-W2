#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
gh release download w2-v1 -R yaleyoou/910C-3P1D-W2 --pattern 'image.seg-*' --pattern '*.tar.gz' --skip-existing
sha256sum -c release.sha256
awk '$2 ~ /^image[.]seg-/ {print $2}' release.sha256 | xargs cat > image.tar.zst
sha256sum -c assets.sha256
mkdir -p data
tar -xzf w2-data.tar.gz -C data
tar -xzf memfabric-complete.tar.gz
bash check_assets.sh
