#!/usr/bin/env python3
"""Numbers pass, step 2: fine-tune a small vision model (Qwen2.5-VL-3B, QLoRA with Unsloth) to read only the line
quantities and unit prices of a purchase order. Runs in the training venv. Saves the LoRA adapter only: the numbers pass
runs the base model + adapter directly in PyTorch (no Ollama conversion, so nothing is lost there).

    numbers_train.py --data /srv/ml/numbers/data --out /srv/ml/numbers/run-<stamp> [--epochs 2]
"""
import argparse, json
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--base", default="unsloth/Qwen2.5-VL-3B-Instruct-bnb-4bit")
ap.add_argument("--epochs", type=float, default=2)
ap.add_argument("--rank", type=int, default=16)
ap.add_argument("--lr", type=float, default=2e-4)
ap.add_argument("--max-seq", type=int, default=8192)
ap.add_argument("--max-steps", type=int, default=0, help="smoke test: stop after N steps")
ap.add_argument("--limit", type=int, default=0, help="smoke test: first N orders only")
a = ap.parse_args()

from unsloth import FastVisionModel, is_bf16_supported          # noqa: E402
from unsloth.trainer import UnslothVisionDataCollator            # noqa: E402
from trl import SFTTrainer, SFTConfig                            # noqa: E402
from PIL import Image                                            # noqa: E402

data, out = Path(a.data), Path(a.out)
out.mkdir(parents=True, exist_ok=True)
rows = [json.loads(l) for l in open(data / "train.jsonl", encoding="utf-8") if l.strip()]
if a.limit:
    rows = rows[:a.limit]
print(f"{len(rows)} training orders", flush=True)
dataset = [{"messages": [
    {"role": "user", "content": [{"type": "image", "image": Image.open(data / r["image"]).convert("RGB")},
                                 {"type": "text", "text": r["prompt"]}]},
    {"role": "assistant", "content": [{"type": "text", "text": r["answer"]}]}]} for r in rows]

model, tokenizer = FastVisionModel.from_pretrained(a.base, load_in_4bit=True, use_gradient_checkpointing="unsloth",
                                                  max_seq_length=a.max_seq)
model = FastVisionModel.get_peft_model(model, finetune_vision_layers=True, finetune_language_layers=True,
                                       finetune_attention_modules=True, finetune_mlp_modules=True,
                                       r=a.rank, lora_alpha=a.rank, lora_dropout=0, bias="none", random_state=3407)
FastVisionModel.for_training(model)
import inspect                                                   # noqa: E402
kw = dict(per_device_train_batch_size=1, gradient_accumulation_steps=8, warmup_steps=10, num_train_epochs=a.epochs,
          learning_rate=a.lr, fp16=not is_bf16_supported(), bf16=is_bf16_supported(), logging_steps=10,
          optim="adamw_8bit", weight_decay=0.01, lr_scheduler_type="linear", seed=3407,
          output_dir=str(out / "checkpoints"), report_to="none", save_strategy="no",
          remove_unused_columns=False, dataset_text_field="", dataset_kwargs={"skip_prepare_dataset": True})
if a.max_steps:
    kw["max_steps"] = a.max_steps
sig = inspect.signature(SFTConfig.__init__).parameters
kw["max_length" if "max_length" in sig else "max_seq_length"] = a.max_seq   # renamed in newer trl
tkw = {"processing_class" if "processing_class" in inspect.signature(SFTTrainer.__init__).parameters else "tokenizer": tokenizer}
# resize="max": train on the full 1600 px page (the default "min" shrank pages to 512 px wide - too small
# for handwritten digits, and not what the model sees when it reads)
trainer = SFTTrainer(model=model, data_collator=UnslothVisionDataCollator(model, tokenizer, resize="max", max_seq_length=a.max_seq), train_dataset=dataset,
                     args=SFTConfig(**kw), **tkw)
stats = trainer.train()
print("training finished:", stats, flush=True)
model.save_pretrained(str(out / "lora"))
tokenizer.save_pretrained(str(out / "lora"))
(out / "train_info.json").write_text(json.dumps({"base": a.base, "orders": len(rows), "epochs": a.epochs, "rank": a.rank,
                                                 "lr": a.lr, "train_loss": getattr(stats, "training_loss", None)}, indent=1))
print("adapter saved:", out / "lora", flush=True)
