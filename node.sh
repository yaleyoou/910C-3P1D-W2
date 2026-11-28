#!/usr/bin/env bash
set -euo pipefail
umask 022
[[ -d /home/miniconda3/bin ]] && export PATH="/home/miniconda3/bin:$PATH"
cd "$(dirname "$0")"
source cluster.env
[[ -z ${DOCKER_HOST:-} ]] || export DOCKER_HOST
action=${1:?load | check | up | down}
NAME=glm52-3p1d
if [[ $action == load ]]; then
  sha256sum -c <(head -n 1 assets.sha256)
  zstd -dc image.tar.zst | docker load
  docker image inspect "$IMAGE" --format '{{.Id}} {{.Architecture}}'
  exit
fi
if [[ $action == down ]]; then
  test "$(docker inspect -f '{{index .Config.Labels "experiment"}}' "$NAME")" = glm52-3p1d
  docker stop "$NAME"
  docker rm "$NAME"
  exit
fi
[[ $action == up || $action == check ]]
ROLE=${2:?p1|p2|p3|d}
case $ROLE in p1) NODE_IP=${P1:-}; model_override=MODEL_DIR_P1 ;;
  p2) NODE_IP=${P2:-}; model_override=MODEL_DIR_P2 ;;
  p3) NODE_IP=${P3:-}; model_override=MODEL_DIR_P3 ;;
  d) NODE_IP=${D:-}; model_override=MODEL_DIR_D ;; *) exit 2 ;; esac
[[ -n $NODE_IP ]] || { echo "$ROLE: IP is empty in cluster.env" >&2; exit 2; }
python3 tools/verify_runtime.py
python3 - "${P1:-}" "${P2:-}" "${P3:-}" "${D:-}" <<'PYIP'
import ipaddress, sys
p1, p2, p3, d = sys.argv[1:5]
if not p1 or not d:
    sys.exit("cluster.env: P1 and D must be non-empty IPv4 addresses")
raw = [s for s in (p1, p2, p3, d) if s]
ips = [ipaddress.IPv4Address(s) for s in raw]
if len(set(ips)) != len(ips) or any(ip.is_loopback or ip.is_unspecified or ip.is_multicast for ip in ips):
    sys.exit("cluster.env: configured P/D hosts must be distinct host IPv4 addresses")
PYIP
if [[ $IFACE == auto ]]; then
  IFACE=$(ip -4 -o addr show | awk -v ip="$NODE_IP" '{split($4,a,"/"); if(a[1]==ip){sub(/@.*/,"",$2);print $2}}')
fi
if [[ -z $IFACE ]] || ! ip -4 -o addr show dev "$IFACE" | grep -F " $NODE_IP/" >/dev/null; then
  echo "$ROLE: $NODE_IP is not on the selected local interface ($IFACE); check cluster.env" >&2; exit 1
fi
MODEL_DIR=${!model_override:-$MODEL_DIR}
[[ $MODEL_DIR == /* && -s "$MODEL_DIR/config.json" ]] || { echo "$ROLE: missing $MODEL_DIR/config.json or path not absolute" >&2; exit 1; }
python3 tools/check_model.py "$MODEL_DIR"
docker image inspect "$IMAGE" >/dev/null
devices=()
for dev in /dev/davinci{0..15} /dev/davinci_manager /dev/devmm_svm /dev/hisi_hdc; do
  [[ -c $dev ]] || { echo "$ROLE: missing device $dev" >&2; exit 1; }
  devices+=(--device "$dev")
done
mounts=()
for path in /usr/local/bin/npu-smi /etc/hccn.conf /etc/ascend_install.info /usr/local/Ascend/driver /usr/local/Ascend/firmware /usr/local/dcmi; do
  [[ -e $path ]] || { echo "$ROLE: missing host mount $path" >&2; exit 1; }; mounts+=(-v "$path:$path:ro")
done
P_DP=${P_DP:-1}
case $P_DP in 1|2|4|8|16) ;; *) echo "cluster.env: P_DP=$P_DP must divide 16 (1/2/4/8/16)" >&2; exit 1 ;; esac
P_TP=$((16 / P_DP))
D_DP=${D_DP:-1}
case $D_DP in 1|2|4|8|16) ;; *) echo "cluster.env: D_DP=$D_DP must divide 16 (1/2/4/8/16)" >&2; exit 1 ;; esac
D_TP=$((16 / D_DP))
max_sparse=$((4096 / D_TP))
if [[ -n ${3:-} ]]; then
  read -r D_STORE SPARSE CASE_P_STORE < <(awk -v g="$3" '$1 !~ /^#/ && $1 != "case" && $1==g{print $2,$3,$4; exit}' cases.tsv)
  [[ -n ${D_STORE:-} && -n ${SPARSE:-} ]] || { echo "Unknown case '$3' in cases.tsv" >&2; exit 1; }
  if [[ -n ${CASE_P_STORE:-} ]]; then P_STORE=$CASE_P_STORE; fi
  ((SPARSE > 0 && SPARSE <= max_sparse)) || { echo "$ROLE: D_sparse_GiB=$SPARSE must be in 1..$max_sparse (DP=$D_DP TP=$D_TP 4 TiB MemFabric limit)" >&2; exit 1; }
  if [[ $ROLE == d ]] && ((D_STORE > 0)); then
    if /usr/local/bin/npu-smi info -t device-share -i 0 -c 0 2>/dev/null | grep -qi 'false'; then
      echo "d: NPU device-share is disabled (required when D_STORE=$D_STORE > 0); see README section 1" >&2
      exit 1
    fi
  fi
fi
if [[ $ROLE == d ]]; then
  echo "$ROLE: $NODE_IP on $IFACE, $MODEL_DIR -> /models/glm-q (D_DP=$D_DP D_TP=$D_TP EP=16)"
else
  echo "$ROLE: $NODE_IP on $IFACE, $MODEL_DIR -> /models/glm-q (P_DP=$P_DP P_TP=$P_TP EP=16)"
fi
[[ $action != check ]] || exit 0
CASE=${3:?F1}; PHASE=${4:?cal|perf|blocks}; TRIAL=${5:?r1}
case $PHASE in cal|perf|blocks) ;; *) exit 2 ;; esac
[[ $TRIAL =~ ^[A-Za-z0-9_-]+$ ]]
output="$PWD/runs/$CASE/$TRIAL/$PHASE/$ROLE"
mkdir -p "$(dirname "$output")"; mkdir "$output"; mkdir "$output/logs"
chmod a+rX "$PWD/runs" "$PWD/runs/$CASE" "$PWD/runs/$CASE/$TRIAL" "$PWD/runs/$CASE/$TRIAL/$PHASE" "$output" "$output/logs" 2>/dev/null || true
cp cluster.env cases.tsv "$output/"
export P1 P2 P3 D IFACE ROLE PHASE CASE TRIAL NODE_IP P_STORE SPARSE D_STORE P_DP D_DP
args=(); for var in P1 P2 P3 D IFACE ROLE PHASE CASE TRIAL NODE_IP P_STORE SPARSE D_STORE P_DP D_DP; do args+=(-e "$var"); done
docker run -d --name "$NAME" --label experiment=glm52-3p1d --network host --shm-size 64g --ulimit memlock=-1:-1 \
  "${devices[@]}" "${mounts[@]}" "${args[@]}" \
  -v "$PWD:/deploy:ro" -v "$output:/run-data" -v "$MODEL_DIR:/models/glm-q:ro" \
  -v "glm52-3p1d-cache-$ROLE:/root/.cache" --entrypoint bash "$IMAGE" -c 'trap "exit 0" TERM INT; while :; do sleep 5 & wait $!; done'
docker exec "$NAME" python3 /deploy/tools/logger_fix.py
inside() { docker exec -d "$NAME" bash "/deploy/services/$1.sh"; }
if [[ $ROLE == p1 ]]; then inside master; fi
for i in {1..90}; do curl --noproxy '*' -fsS "http://$P1:50089/metrics" -o /dev/null && break; sleep 2; done
curl --noproxy '*' -fsS "http://$P1:50089/metrics" -o /dev/null
if [[ $ROLE == d ]]; then
  inside d
  if ((D_STORE > 0)); then inside store_d; fi
else
  inside p
  if [[ $ROLE == p1 ]]; then inside proxy; fi
fi
echo "$ROLE logs: $output/logs"

