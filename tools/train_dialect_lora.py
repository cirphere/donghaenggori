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
    """오디오를 **배치마다 그 자리에서** mel 로 바꾼다.

    미리 만들어 두지 않는 이유가 있다. mel 은 샘플당 128×3000 float32 = 1.5MB
    라, 5,498 조각이면 8.4GB 다. datasets.map 으로 만들면 워커별 결과를 합치는
    마지막 단계에서 그 전부가 메모리에 올라와 OOM 으로 죽는다 — 클러스터에서
    실제로 겪었다(32GB 한도, 전처리 100% 직후 oom-kill).

    여기서 계산하면 배치 하나분(8×1.5MB)만 들고 있으면 된다. 디스크에 캐시를
    쓰지 않아 8.4GB 도 아낀다. 대신 에폭마다 다시 계산하는데, DataLoader
    워커가 GPU 계산과 겹쳐 돌려 주므로 실질 손해가 거의 없다.

    라벨의 패딩 자리는 -100 이다. 안 그러면 모델이 패딩 토큰을 예측하도록 배운다.
    """
    processor: Any
    decoder_start_token_id: int

    def __call__(self, features: list[dict]) -> dict:
        import soundfile as sf

        wavs = []
        for f in features:
            wav, sr = sf.read(f["path"], dtype="float32", always_2d=False)
            if wav.ndim > 1:                   # 혹시 스테레오가 섞이면 모노로
                wav = wav.mean(axis=1)
            if sr != 16000:
                # 리샘플링은 하지 않는다 — 학습셋을 만들 때 맞췄어야 하는 것이고,
                # 여기서 조용히 고치면 채널이 어긋난 채로 학습이 돈다.
                raise ValueError(f"16kHz 가 아니다: {sr}Hz — {f['path']}")
            wavs.append(wav)

        batch = self.processor.feature_extractor(
            wavs, sampling_rate=16000, return_tensors="pt")
        labels_batch = self.processor.tokenizer(
            [f["text"] for f in features], padding=True, return_tensors="pt")
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

    # **경로와 텍스트만 담는다.** 오디오 디코딩과 mel 변환은 Collator 가
    # 배치마다 한다(위 주석 참조 — 미리 만들면 8.4GB 가 메모리에 올라와 죽는다).
    #
    # datasets 의 Audio 기능도 쓰지 않는다. 그건 디코딩에 librosa 를 요구하는데,
    # librosa 는 numba·llvmlite 를 끌고 와서 파이썬 버전이 조금만 어긋나도
    # 소스 빌드로 넘어가 깨진다 — 계산 노드가 3.8 인 이 클러스터에서 그랬다.
    ds = Dataset.from_list([{"path": r["audio"], "text": r["text"]} for r in rows])
    split = ds.train_test_split(test_size=args.eval_ratio, seed=7)

    model = WhisperForConditionalGeneration.from_pretrained(
        args.model, torch_dtype=torch.bfloat16)
    model.config.forced_decoder_ids = None
    model.config.suppress_tokens = []
    # 재계산으로 활성값 메모리를 줄인다. 30초 입력이라 이게 없으면 배치를 못 키운다.
    model.config.use_cache = False

    # **use_reentrant=False 가 반드시 필요하다.**
    #
    # 기본값(reentrant)은 checkpoint 구간의 **입력**이 requires_grad 가 아니면
    # 그 구간의 역전파를 통째로 건너뛴다. 안에 있는 LoRA 파라미터도 같이
    # 건너뛴다. Whisper 인코더는 mel(requires_grad=False)을 conv 로 받으므로
    # 정확히 그 조건에 걸린다 — enable_input_require_grads() 는
    # get_input_embeddings(), 즉 **디코더** 임베딩에만 훅을 걸어서 인코더에는
    # 닿지 않는다.
    #
    # 그러면 디코더만 학습되고 인코더 LoRA 는 0 인 채로 끝난다. 방언 적응은
    # 발음을 배우는 일이라 인코더가 핵심인데, 정작 그쪽이 안 배운다.
    #
    # 증상으로 드러난다: 로그에 "None of the inputs have requires_grad=True.
    # Gradients will be None" 이 뜨고, 스텝이 비정상적으로 빨라진다(A6000 에서
    # 1.19s/it — 인코더 역전파가 없어야 나오는 속도다).
    model.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()

    # q·v 만 건드리는 것이 Whisper 적응의 표준 구성이다. 넓힐수록 망각 위험이 는다.
    model = get_peft_model(model, LoraConfig(
        r=args.rank, lora_alpha=args.alpha, lora_dropout=0.05, bias="none",
        target_modules=["q_proj", "v_proj"]))
    model.print_trainable_parameters()

    def assert_encoder_learns(collator: Collator, sample: list[dict]) -> None:
        """인코더에 기울기가 실제로 흐르는지 **학습 전에** 한 배치로 확인한다.

        조용히 안 배우고 끝나는 것이 이 설정에서 가장 비싼 실패다. 학습은
        정상으로 보이고 loss 도 내려가지만(디코더가 배운다) 정작 원하던
        방언 발음 적응은 일어나지 않는다. 몇 시간 뒤 CER 이 그대로인 것을
        보고서야 알게 되는데, 그때는 원인을 좁히기 어렵다.

        배치 하나분(2~3초)으로 끝나므로 매번 켜 둘 값어치가 있다.
        """
        # mel 은 float32 로 나오는데 모델은 bf16 이다. 학습 중에는 Trainer 가
        # 맞춰 주지만 여기서는 직접 부르므로 우리가 맞춰야 한다 — 라벨은
        # 정수라 건드리지 않는다.
        batch = collator(sample)
        batch = {k: (v.to(model.device, model.dtype) if v.is_floating_point()
                     else v.to(model.device))
                 for k, v in batch.items()}
        model.train()
        model(**batch).loss.backward()
        enc = [p for n, p in model.named_parameters()
               if p.requires_grad and ".encoder." in n]
        got = sum(1 for p in enc if p.grad is not None and p.grad.abs().sum() > 0)
        model.zero_grad(set_to_none=True)
        print(f"[검증] 인코더 LoRA {len(enc)}개 중 기울기 있는 것 {got}개", flush=True)
        if enc and got == 0:
            raise RuntimeError(
                "인코더 LoRA 에 기울기가 흐르지 않는다. gradient checkpointing 의 "
                "use_reentrant 를 확인하라 — 이대로면 디코더만 학습된다.")

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
            # **여기에도 줘야 한다.** 위에서 모델에 직접 걸어 둔 것을 Trainer 가
            # 자기 기본값(reentrant)으로 다시 호출해 덮어쓴다. 그러면 인코더
            # LoRA 에 기울기가 안 흐르는 상태로 되돌아간다 — 로그에 reentrant
            # 경로(checkpoint.py:92)와 비-reentrant 경로(:295)가 **둘 다**
            # 찍히는 것으로 드러났다.
            #
            # 학습 전 검증(assert_encoder_learns)은 Trainer 밖에서 재므로
            # 이 상태를 못 잡는다. 검증이 통과하는데 학습은 틀린 구성으로
            # 도는, 가장 속기 쉬운 형태였다.
            gradient_checkpointing_kwargs={"use_reentrant": False},
            logging_steps=25,
            eval_strategy="steps",
            eval_steps=200,
            save_steps=200,
            # 월타임에 잘려도 이어서 돌릴 수 있게 남긴다. 클러스터에서는
            # 이것이 없으면 끊긴 학습을 처음부터 다시 해야 한다.
            save_total_limit=3,
            report_to=[],
            # Collator 가 원본 컬럼(path·text)을 읽으므로 지우면 안 된다.
            remove_unused_columns=False,
            label_names=["labels"],
            # mel 변환을 GPU 계산과 겹쳐 돌린다. 2 로 둔 것은 CPU 한도가
            # 4개뿐이라서다 — 더 올리면 메인 프로세스가 굶는다.
            dataloader_num_workers=2,
        ),
        train_dataset=split["train"],
        eval_dataset=split["test"],
        data_collator=Collator(processor, model.config.decoder_start_token_id),
    )

    model.to(trainer.args.device)
    assert_encoder_learns(trainer.data_collator,
                          [split["train"][0], split["train"][1]])

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
