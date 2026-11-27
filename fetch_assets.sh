#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
base=https://github.com/yaleyoou/910C-3P1D-W2/releases/download/w2-v1
# Git may already know the cluster's CA bundle used for HTTPS cloning.
if [[ -z ${CURL_CA_BUNDLE:-} && -z ${SSL_CERT_FILE:-} && -z ${SSL_CERT_DIR:-} ]]; then
  git_ca=${GIT_SSL_CAINFO:-}
  if [[ -z $git_ca ]] && command -v git >/dev/null; then
    git_ca=$(git config --path --get-urlmatch http.sslCAInfo "$base" || true)
  fi
  if [[ -n $git_ca && -r $git_ca ]]; then export CURL_CA_BUNDLE="$git_ca"; fi
fi
if [[ -z ${https_proxy:-} && -z ${HTTPS_PROXY:-} && -z ${ALL_PROXY:-} && -z ${all_proxy:-} ]] && command -v git >/dev/null; then
  git_proxy=$(git config --get-urlmatch http.proxy "$base" || true)
  if [[ -n $git_proxy ]]; then export https_proxy="$git_proxy"; fi
fi
if [[ ${1:-} == --probe ]]; then
  echo "Testing first image segment (HTTPS byte range 0-0)"
  curl -q -fsSL --range 0-0 --max-filesize 1024 --connect-timeout 10 --max-time 40 \
    -D - -o /dev/null "$base/image.seg-0000" | awk '/^HTTP\// {sub(/\r$/, ""); print; fflush()}'
  exit
fi
tmp=$(mktemp -d .fetch-assets.XXXXXX)
trap 'rm -rf -- "$tmp"' EXIT
have_image=false
if [[ -f image.tar.zst ]] && grep '  image.tar.zst$' assets.sha256 | sha256sum -c --status; then
  have_image=true
fi
while read -r digest name; do
  if [[ $have_image == true && $name == image.seg-* ]]; then continue; fi
  if [[ -f $name ]] && printf '%s  %s\n' "$digest" "$name" | sha256sum -c --status; then continue; fi
  echo "Downloading $name"
  curl -q -fLsS --retry 3 --connect-timeout 20 "$base/$name" -o "$tmp/$name"
  printf '%s  %s\n' "$digest" "$tmp/$name" | sha256sum -c -
  mv -- "$tmp/$name" "$name"
done < release.sha256
if [[ $have_image == false ]]; then
  awk '$2 ~ /^image[.]seg-/ {print $2}' release.sha256 | xargs cat > "$tmp/image.tar.zst"
  mv -- "$tmp/image.tar.zst" image.tar.zst
fi
sha256sum -c assets.sha256
mkdir -p data
tar -xzf w2-data.tar.gz -C data
tar -xzf memfabric-complete.tar.gz
bash check_assets.sh
