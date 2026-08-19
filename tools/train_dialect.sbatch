#!/bin/bash
# Whisper 방언 LoRA 파인튜닝 — Slurm 제출 스크립트
#
#   sbatch tools/train_dialect.sbatch
#
# 파티션은 sinfo 로 확인해 맞춘다. 확인된 것:
#   p01  A6000  무제한      p02(기본)  A6000  14일
#   p03  A100×8 무제한      p04·p05    Q8000  무제한
# A100 을 쓸 수 있으면 p03 이 가장 빠르다. 아래는 A6000 기준이다.
#
#SBATCH --job-name=dhg-dialect-lora
#SBATCH --partition=p01
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --output=logs/lora-%j.out
#SBATCH --error=logs/lora-%j.err

set -euo pipefail
mkdir -p logs

# 계산 노드는 인터넷이 막힌 곳이 많다. 모델을 미리 받아 두고 오프라인으로 돈다.
# (로그인 노드에서 한 번:  huggingface-cli download openai/whisper-large-v3)
export HF_HOME="${HF_HOME:-$PWD/.hf}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
export TOKENIZERS_PARALLELISM=false

nvidia-smi --query-gpu=name,memory.total --format=csv,noheader

# **드라이버 525(CUDA 12.0) 다.** torch 는 cu121 로 맞춘다 — 최신 빌드를 깔면
# 커널이 안 맞아 'no kernel image is available' 로 죽는다.
python -c "import torch; print('torch', torch.__version__, '| cuda', torch.version.cuda, '| avail', torch.cuda.is_available())"

python -m tools.train_dialect_lora \
    --manifest "${MANIFEST:-data/dialect/train/manifest.jsonl}" \
    --out "${OUT:-out/lora-v1}" \
    --epochs "${EPOCHS:-2}" \
    --batch "${BATCH:-8}" \
    --accum "${ACCUM:-2}" \
    --merge

# 학습이 끝나면 배포 포맷으로 변환한다. faster-whisper 는 CTranslate2 만 읽는다.
OUT_DIR="${OUT:-out/lora-v1}"
if [ -d "$OUT_DIR/merged" ]; then
    ct2-transformers-converter --model "$OUT_DIR/merged" \
        --output_dir "$OUT_DIR/ct2" \
        --copy_files preprocessor_config.json tokenizer.json \
        --quantization float16
    echo "배포 경로: $OUT_DIR/ct2  →  WHISPER_MODEL 에 이 경로를 준다"
fi
