#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source cluster.env
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
case $ROLE in p1) NODE_IP=$P1; model_override=MODEL_DIR_P1 ;;
  p2) NODE_IP=$P2; model_override=MODEL_DIR_P2 ;;
  p3) NODE_IP=$P3; model_override=MODEL_DIR_P3 ;;
  d) NODE_IP=$D; model_override=MODEL_DIR_D ;; *) exit 2 ;; esac
python3 tools/verify_runtime.py
python3 - "$P1" "$P2" "$P3" "$D" <<'PYIP'
import ipaddress, sys
ips = [ipaddress.IPv4Address(s) for s in sys.argv[1:]]
if len(set(ips)) != 4 or any(ip.is_loopback or ip.is_unspecified or ip.is_multicast for ip in ips):
    sys.exit("cluster.env: P1/P2/P3/D must be four distinct host IPv4 addresses")
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
echo "$ROLE: $NODE_IP on $IFACE, $MODEL_DIR -> /models/glm-q"
[[ $action != check ]] || exit 0
CASE=${3:?F1}; PHASE=${4:?cal|perf|blocks}; TRIAL=${5:?r1}
case $PHASE in cal|perf|blocks) ;; *) exit 2 ;; esac
[[ $TRIAL =~ ^[A-Za-z0-9_-]+$ ]]
read -r D_STORE SPARSE < <(awk -v g="$CASE" '$1==g{print $2,$3}' cases.tsv)
output="$PWD/runs/$CASE/$TRIAL/$PHASE/$ROLE"
mkdir -p "$(dirname "$output")"; mkdir "$output"; mkdir "$output/logs"
cp cluster.env cases.tsv "$output/"
export P1 P2 P3 D IFACE ROLE PHASE CASE TRIAL NODE_IP P_STORE SPARSE D_STORE
args=(); for var in P1 P2 P3 D IFACE ROLE PHASE CASE TRIAL NODE_IP P_STORE SPARSE D_STORE; do args+=(-e "$var"); done
docker run -d --name "$NAME" --label experiment=glm52-3p1d --network host --init --shm-size 64g --ulimit memlock=-1:-1 \
  "${devices[@]}" "${mounts[@]}" "${args[@]}" \
  -v "$PWD:/deploy:ro" -v "$output:/run-data" -v "$MODEL_DIR:/models/glm-q:ro" \
  -v "glm52-3p1d-cache-$ROLE:/root/.cache" --entrypoint sleep "$IMAGE" infinity
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

