#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
IMAGE=neteast-spk-server:latest
NAMESPACE=${DOCKERHUB_NAMESPACE-pczhao1210}
BUILD=0
PUSH=0

usage() {
  printf '%s\n' \
    'Usage: ./start.sh build|--build [push|--push] [--namespace USER_OR_ORG]' \
    '       ./start.sh push|--push [--namespace USER_OR_ORG]' \
    '' \
    'build: build the native-platform runtime and load neteast-spk-server:latest.' \
    'push:  build ARM64 + AMD64 and publish docker.io/NAMESPACE/neteast-spk-server:latest.' \
    'Default namespace: pczhao1210. Override with DOCKERHUB_NAMESPACE or --namespace.' \
    'Run docker login first; passwords/tokens are never accepted by this script.' \
    'Create that repository as PRIVATE on Docker Hub before pushing.' \
    'The script does not create repositories or change their visibility.'
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 2
}

while (( $# )); do
  case "$1" in
    build|--build) BUILD=1 ;;
    push|--push) PUSH=1 ;;
    --namespace)
      (( $# >= 2 )) || die '--namespace requires a Docker Hub user or organization.'
      NAMESPACE=$2
      shift
      ;;
    --namespace=*) NAMESPACE=${1#--namespace=} ;;
    -h|--help|help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
  shift
done

if (( !BUILD && !PUSH )); then
  usage
  exit 0
fi

if (( PUSH )); then
  [[ "$NAMESPACE" =~ ^[a-z0-9][a-z0-9_-]{0,38}$ ]] || \
    die 'Set DOCKERHUB_NAMESPACE or --namespace to your lowercase Docker Hub user/organization.'
fi

command -v docker >/dev/null 2>&1 || die 'Docker CLI is not installed.'
docker info >/dev/null
docker buildx version >/dev/null

if (( PUSH )); then
  REMOTE="docker.io/$NAMESPACE/$IMAGE"
  BUILDER=${SPK_BUILDX_BUILDER:-spk-server-multi}
  [[ "$BUILDER" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || die 'Invalid SPK_BUILDX_BUILDER name.'
  if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
  fi
  PLATFORMS=$(docker buildx inspect "$BUILDER" --bootstrap | sed -n 's/^Platforms:[[:space:]]*//p' \
    | tr -d ' *' | tr '\n' ',')
  for required in linux/arm64 linux/amd64; do
    [[ ",$PLATFORMS," == *",$required,"* ]] || die "Builder $BUILDER cannot build $required. \
On an x64 host install QEMU binfmt first (docker run --privileged --rm tonistiigi/binfmt --install arm64), \
or use a native ARM64 builder node."
  done
  printf 'Publishing %s (linux/arm64, linux/amd64). Repository must already be private.\n' "$REMOTE"
  docker buildx build --builder "$BUILDER" \
    --platform linux/arm64,linux/amd64 \
    --file "$ROOT/Dockerfile" --target runtime \
    --tag "$REMOTE" --push "$ROOT"
  docker buildx imagetools inspect "$REMOTE"
else
  PLATFORM=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')
  case "$PLATFORM" in
    linux/amd64|linux/arm64) ;;
    linux/x86_64) PLATFORM=linux/amd64 ;;
    linux/aarch64) PLATFORM=linux/arm64 ;;
    *) die "Unsupported Docker daemon platform: $PLATFORM" ;;
  esac
  printf 'Building %s for the Docker daemon native platform.\n' "$IMAGE"
  docker buildx build --platform "$PLATFORM" --file "$ROOT/Dockerfile" --target runtime \
    --tag "$IMAGE" --load "$ROOT"
fi