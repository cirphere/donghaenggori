#!/bin/bash
# Whisper 방언 LoRA 파인튜닝 — Slurm 제출 스크립트
#
#   sbatch tools/train_dialect.sbatch
#
# **파티션을 박지 않는다.** 계정마다 쓸 수 있는 곳이 다르다 — p01 을 지정했더니
# "Invalid account or account/partition combination" 으로 거절당했다. 기본
# 파티션에 맡기고, 다른 곳을 쓰려면 제출할 때 -p 로 준다:
#     sbatch -p p04 tools/train_dialect.sbatch
# 쓸 수 있는 곳은 sacctmgr show assoc where user=$USER 로 확인한다.
#
#SBATCH --job-name=dhg-dialect-lora
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

# **파이썬을 절대경로로 부른다.** `python3` 에 맡기면 안 된다 — 이 클러스터는
# 로그인 노드에 3.10, 계산 노드에 3.8 이 있고 /usr/bin/python3 가 노드마다
# 다른 것을 가리킨다. 로그인 노드에서 만든 3.10 venv 를 계산 노드에서 부르면
# 심볼릭 링크가 3.8 로 풀려서 site-packages 를 통째로 못 본다(실제로 겪었다).
PY="${PY:-$HOME/aix/.venv38/bin/python3}"
[ -x "$PY" ] || { echo "파이썬을 찾지 못했다: $PY  (PY=... 로 지정하라)"; exit 1; }

# **드라이버 525(CUDA 12.0) 다.** torch 는 cu121 로 맞춘다 — 최신 빌드를 깔면
# 커널이 안 맞아 'no kernel image is available' 로 죽는다.
"$PY" -c "import sys, torch; print('python', sys.version.split()[0], '| torch', torch.__version__, '| cuda', torch.version.cuda, '| avail', torch.cuda.is_available())"

"$PY" -m tools.train_dialect_lora \
    --manifest "${MANIFEST:-data/dialect/train/manifest.jsonl}" \
    --out "${OUT:-out/lora-v1}" \
    --epochs "${EPOCHS:-2}" \
    --batch "${BATCH:-8}" \
    --accum "${ACCUM:-2}" \
    --merge

# 학습이 끝나면 배포 포맷으로 변환한다. faster-whisper 는 CTranslate2 만 읽는다.
OUT_DIR="${OUT:-out/lora-v1}"
if [ -d "$OUT_DIR/merged" ]; then
    # 같은 venv 의 콘솔 스크립트를 쓴다. ctranslate2 가 아직 없으면 여기서
    # 멈추되 학습 결과(merged)는 남는다 — 변환만 나중에 다시 하면 된다.
    CT2="$(dirname "$PY")/ct2-transformers-converter"
    if [ -x "$CT2" ]; then
        "$CT2" --model "$OUT_DIR/merged" \
            --output_dir "$OUT_DIR/ct2" \
            --copy_files preprocessor_config.json tokenizer.json \
            --quantization float16
        echo "배포 경로: $OUT_DIR/ct2  →  WHISPER_MODEL 에 이 경로를 준다"
    else
        echo "ctranslate2 미설치 — 변환은 건너뛴다."
        echo "  pip install ctranslate2  후:"
        echo "  ct2-transformers-converter --model $OUT_DIR/merged \\"
        echo "      --output_dir $OUT_DIR/ct2 --quantization float16 \\"
        echo "      --copy_files preprocessor_config.json tokenizer.json"
    fi
fi
