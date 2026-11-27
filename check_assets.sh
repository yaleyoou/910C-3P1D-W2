#!/usr/bin/env bash
set -euo pipefail
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"
python3 tools/verify_runtime.py
sha256sum -c assets.sha256
expected=e07158bf61731488e237d2f7a3fe1730779d11c141d045e3c4df8367835e9965
test "$(wc -c < image.tar.zst | tr -d ' ')" = 7007478446
observed=$(zstd -dc image.tar.zst | sha256sum | cut -d' ' -f1)
test "$observed" = "$expected"
echo 'Image archive SHA-256 verified'
python3 tools/verify_w2.py data/checkpoints/W2/frozen

