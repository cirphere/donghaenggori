"""Whisper large-v3 LoRA 파인튜닝 — 전남 어르신 방언.

    python -m tools.train_dialect_lora --manifest train/manifest.jsonl --out out/lora-v1

**왜 LoRA 인가.** 학습 데이터가 26시간이다. 이 규모로 1.5B 를 통째로 돌리면
파괴적 망각이 온다 — 방언은 조금 얻고 그 밖의 말(표준어 문의, 보호자 통화,
숫자·날짜 표현)을 잃는다. 우리 입력은 방언만 오는 것이 아니다. LoRA 는
원본 가중치를 얼려 두므로 그 손실이 구조적으로 작고, 나빠지면 어댑터만
버리면 된다. 부족하다고 판명되면 그때 범위를 넓힌다.

**왜 방언형으로 학습하나.** 정답 텍스트가 '가야겄어' 이지 '가야겠어' 가
아니다. voice_samples/README 가 사람에게 라벨을 고치라고 할 때 건 원칙과
같다 — 표준어로 바꿔 학습하면 방언을 못 배운다.

**배포까지 두 단계가 더 있다.** 우리 서버는 faster-whisper(CTranslate2)를
쓴다. 학습이 끝난 어댑터를 그대로 못 올린다:

    1) merge_and_unload() 로 base 에 병합해 HF 포맷으로 저장
    2) ct2-transformers-converter 로 CTranslate2 포맷 변환
    3) WHISPER_MODEL 에 그 디렉토리 경로를 준다

--merge 를 주면 1) 까지 이 스크립트가 한다. 2) 는 README 에 명령을 적어 둔다.

**GPU 서버 주의.** 드라이버 525(CUDA 12.0)다. torch 는 cu121 로 고정해야
한다 — 최신 빌드(cu126/cu13x)를 깔면 커널이 안 맞아 죽는다.

    pip install torch --index-url https://download.pytorch.org/whl/cu121
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

MODEL = "openai/whisper-large-v3"
LANG, TASK = "ko", "transcribe"


def load_manifest(path: str) -> list[dict]:
    root = os.path.dirname(os.path.abspath(path))
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            r["audio"] = os.path.join(root, r["audio"])
            rows.append(r)
    return rows


@dataclass
class Collator:
    """input_features 는 항상 3000 프레임이라 붙이기만 하면 되고, 라벨만 패딩한다.

    패딩 자리를 -100 으로 둔다. 안 그러면 모델이 패딩 토큰을 예측하도록 배운다.
    """
    processor: Any
    decoder_start_token_id: int

    def __call__(self, features: list[dict]) -> dict:
        batch = self.processor.feature_extractor.pad(
            [{"input_features": f["input_features"]} for f in features],
            return_tensors="pt")
        labels_batch = self.processor.tokenizer.pad(
            [{"input_ids": f["labels"]} for f in features], return_tensors="pt")
        labels = labels_batch["input_ids"].masked_fill(
            labels_batch.attention_mask.ne(1), -100)
        # 토크나이저가 앞에 붙인 BOS 는 학습 때 디코더가 스스로 넣는다.
        if (labels[:, 0] == self.decoder_start_token_id).all().cpu().item():
            labels = labels[:, 1:]
        batch["labels"] = labels
        return batch


def main() -> int:
    ap = argparse.ArgumentParser(description="Whisper 방언 LoRA 파인튜닝")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--accum", type=int, default=2)
    ap.add_argument("--lr", type=float, default=1e-3)      # LoRA 는 풀 파인튜닝보다 크게 준다
    ap.add_argument("--rank", type=int, default=32)
    ap.add_argument("--alpha", type=int, default=64)
    ap.add_argument("--eval-ratio", type=float, default=0.02,
                    help="학습 중 손실만 보기 위한 소량. 진짜 평가는 stt_eval 로 한다")
    ap.add_argument("--merge", action="store_true", help="학습 후 base 에 병합해 저장")
    args = ap.parse_args()

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model
    from transformers import (
        Seq2SeqTrainer,
        Seq2SeqTrainingArguments,
        WhisperForConditionalGeneration,
        WhisperProcessor,
    )

    rows = load_manifest(args.manifest)
    print(f"학습 조각 {len(rows)}개 · {sum(r['duration'] for r in rows)/3600:.2f} 시간")

    processor = WhisperProcessor.from_pretrained(args.model, language=LANG, task=TASK)

    # **datasets 의 Audio 기능을 쓰지 않는다.** 그건 디코딩에 librosa 를
    # 요구하는데(datasets 2.x), librosa 는 numba·llvmlite 를 끌고 와서 파이썬
    # 버전이 조금만 어긋나도 소스 빌드로 넘어가 깨진다 — 클러스터에서 실제로
    # 겪었다. prep_dialect_finetune 이 이미 **16kHz 모노**로 맞춰 두었으므로
    # 리샘플링이 필요 없고, soundfile 로 바로 읽으면 그만이다.
    ds = Dataset.from_list([{"path": r["audio"], "text": r["text"]} for r in rows])

    def prepare(batch: dict) -> dict:
        import soundfile as sf
        wav, sr = sf.read(batch["path"], dtype="float32", always_2d=False)
        if wav.ndim > 1:                       # 혹시 스테레오가 섞이면 모노로
            wav = wav.mean(axis=1)
        if sr != 16000:
            # 리샘플링은 하지 않는다 — 학습셋을 만들 때 맞췄어야 하는 것이고,
            # 여기서 조용히 고치면 채널이 어긋난 채로 학습이 돈다.
            raise ValueError(f"16kHz 가 아니다: {sr}Hz — {batch['path']}")
        batch["input_features"] = processor.feature_extractor(
            wav, sampling_rate=sr).input_features[0]
        batch["labels"] = processor.tokenizer(batch["text"]).input_ids
        return batch

    ds = ds.map(prepare, remove_columns=ds.column_names, num_proc=4)
    split = ds.train_test_split(test_size=args.eval_ratio, seed=7)

    model = WhisperForConditionalGeneration.from_pretrained(
        args.model, torch_dtype=torch.bfloat16)
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []
    # 재계산으로 활성값 메모리를 줄인다. 30초 입력이라 이게 없으면 배치를 못 키운다.
    model.config.use_cache = False
    model.gradient_checkpointing_enable()
    model.enable_input_require_grads()

    # q·v 만 건드리는 것이 Whisper 적응의 표준 구성이다. 넓힐수록 망각 위험이 는다.
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.alpha, lora_dropout=0.05, bias="none",
        target_modules=["q_proj", "v_proj"]))
    model.print_trainable_parameters()

    trainer = Seq2SeqTrainer(
        model=model,
        args=Seq2SeqTrainingArguments(
            output_dir=args.out,
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=args.accum,
            learning_rate=args.lr,
            warmup_ratio=0.05,
            num_train_epochs=args.epochs,
            bf16=True,
            gradient_checkpointing=True,
            logging_steps=25,
            eval_strategy="steps",
            eval_steps=200,
            save_steps=200,
            # 월타임에 잘려도 이어서 돌릴 수 있게 남긴다. 클러스터에서는
            # 이것이 없으면 끊긴 학습을 처음부터 다시 해야 한다.
            save_total_limit=3,
            report_to=[],
            remove_unused_columns=False,
            label_names=["labels"],
        ),
        train_dataset=split["train"],
        eval_dataset=split["test"],
        data_collator=Collator(processor, model.config.decoder_start_token_id),
    )

    resume = bool(args.out and os.path.isdir(args.out)
                  and any(d.startswith("checkpoint-") for d in os.listdir(args.out)))
    trainer.train(resume_from_checkpoint=resume or None)
    trainer.save_model(os.path.join(args.out, "adapter"))
    processor.save_pretrained(os.path.join(args.out, "adapter"))
    print(f"어댑터 저장 → {args.out}/adapter")

    if args.merge:
        merged = os.path.join(args.out, "merged")
        base = WhisperForConditionalGeneration.from_pretrained(
            args.model, torch_dtype=torch.float16)
        from peft import PeftModel
        m = PeftModel.from_pretrained(base, os.path.join(args.out, "adapter"))
        m = m.merge_and_unload()
        m.save_pretrained(merged)
        processor.save_pretrained(merged)
        print(f"병합 모델 저장 → {merged}")
        print("배포용 변환:")
        print(f"  ct2-transformers-converter --model {merged} \\")
        print(f"      --output_dir {os.path.join(args.out, 'ct2')} \\")
        print("      --copy_files preprocessor_config.json tokenizer.json "
              "--quantization float16")
    return 0


if __name__ == "__main__":
    sys.exit(main())
