#!/usr/bin/env python3
"""Fine-tune Qwen2.5-VL-7B (QLoRA, Unsloth) on checked purchase orders. Started by training.py finetune, inside the
training venv (setup_training.sh). Reads DATA/train.jsonl ({"image","prompt","answer"}), writes OUT/lora (the adapter)
and OUT/merged (full 16-bit weights, for `ollama create`).

Sized for a 16 GB card: 4-bit base, LoRA rank 16, batch 1 x 8 accumulation, gradient checkpointing, images ~1288 px.
"""
import argparse, json, os
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--data", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--base", default="unsloth/Qwen2.5-VL-7B-Instruct-bnb-4bit")
ap.add_argument("--epochs", type=float, default=2)
ap.add_argument("--rank", type=int, default=16)
ap.add_argument("--lr", type=float, default=2e-4)
ap.add_argument("--max-seq", type=int, default=6144)
a = ap.parse_args()

from unsloth import FastVisionModel, is_bf16_supported          # noqa: E402  (imported after argparse: slow import)
from unsloth.trainer import UnslothVisionDataCollator            # noqa: E402
from trl import SFTTrainer, SFTConfig                            # noqa: E402
from PIL import Image                                            # noqa: E402

data, out = Path(a.data), Path(a.out)
rows = [json.loads(l) for l in open(data / "train.jsonl", encoding="utf-8") if l.strip()]
print(f"{len(rows)} training examples", flush=True)


def convert(r):
    return {"messages": [
        {"role": "user", "content": [{"type": "image", "image": Image.open(data / r["image"]).convert("RGB")},
                                     {"type": "text", "text": r["prompt"]}]},
        {"role": "assistant", "content": [{"type": "text", "text": r["answer"]}]}]}


dataset = [convert(r) for r in rows]

model, tokenizer = FastVisionModel.from_pretrained(a.base, load_in_4bit=True, use_gradient_checkpointing="unsloth")
model = FastVisionModel.get_peft_model(model, finetune_vision_layers=True, finetune_language_layers=True,
                                       finetune_attention_modules=True, finetune_mlp_modules=True,
                                       r=a.rank, lora_alpha=a.rank, lora_dropout=0, bias="none", random_state=3407)
FastVisionModel.for_training(model)
trainer = SFTTrainer(
    model=model, tokenizer=tokenizer, data_collator=UnslothVisionDataCollator(model, tokenizer), train_dataset=dataset,
    args=SFTConfig(per_device_train_batch_size=1, gradient_accumulation_steps=8, warmup_steps=5, num_train_epochs=a.epochs,
                   learning_rate=a.lr, fp16=not is_bf16_supported(), bf16=is_bf16_supported(), logging_steps=5,
                   optim="adamw_8bit", weight_decay=0.01, lr_scheduler_type="linear", seed=3407,
                   output_dir=str(out / "checkpoints"), report_to="none", save_strategy="epoch", save_total_limit=1,
                   remove_unused_columns=False, dataset_text_field="", dataset_kwargs={"skip_prepare_dataset": True},
                   max_seq_length=a.max_seq))
stats = trainer.train()
print("training finished:", stats, flush=True)
model.save_pretrained(str(out / "lora"))
tokenizer.save_pretrained(str(out / "lora"))
print("saving merged 16-bit weights (downloads the full base model the first time)", flush=True)
model.save_pretrained_merged(str(out / "merged"), tokenizer, save_method="merged_16bit")
print("done", flush=True)
