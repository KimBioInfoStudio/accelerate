# coding=utf-8
# Copyright 2021 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import argparse
import os

import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader

import evaluate
from accelerate import Accelerator, DistributedType
from datasets import load_dataset
from transformers import AutoModelForSequenceClassification, AutoTokenizer, get_linear_schedule_with_warmup, set_seed


########################################################################
# This is a fully working simple example to use Accelerate,
# specifically showcasing the checkpointing capability,
# and builds off the `nlp_example.py` script.
#
# This example trains a Bert base model on GLUE MRPC
# in any of the following settings (with the same script):
#   - single CPU or single GPU
#   - multi GPUS (using PyTorch distributed mode)
#   - (multi) TPUs
#   - fp16 (mixed-precision) or fp32 (normal precision)
#
# To help focus on the differences in the code, building `DataLoaders`
# was refactored into its own function.
# New additions from the base script can be found quickly by
# looking for the # New Code # tags
#
# To run it in each of these various modes, follow the instructions
# in the readme for examples:
# https://github.com/huggingface/accelerate/tree/main/examples
#
########################################################################

MAX_GPU_BATCH_SIZE = 16
EVAL_BATCH_SIZE = 32


def get_dataloaders(accelerator: Accelerator, batch_size: int = 16):
    """
    Creates a set of `DataLoader`s for the `glue` dataset,
    using "bert-base-cased" as the tokenizer.

    Args:
        accelerator (`Accelerator`):
            An `Accelerator` object
        batch_size (`int`, *optional*):
            The batch size for the train and validation DataLoaders.
    """
    tokenizer = AutoTokenizer.from_pretrained("bert-base-cased")
    datasets = load_dataset("glue", "mrpc")

    def tokenize_function(examples):
        # max_length=None => use the model max length (it's actually the default)
        outputs = tokenizer(examples["sentence1"], examples["sentence2"], truncation=True, max_length=None)
        return outputs

    # Apply the method we just defined to all the examples in all the splits of the dataset
    tokenized_datasets = datasets.map(
        tokenize_function,
        batched=True,
        remove_columns=["idx", "sentence1", "sentence2"],
    )

    # We also rename the 'label' column to 'labels' which is the expected name for labels by the models of the
    # transformers library
    tokenized_datasets = tokenized_datasets.rename_column("label", "labels")

    def collate_fn(examples):
        # On TPU it's best to pad everything to the same length or training will be very slow.
        if accelerator.distributed_type == DistributedType.TPU:
            return tokenizer.pad(examples, padding="max_length", max_length=128, return_tensors="pt")
        return tokenizer.pad(examples, padding="longest", return_tensors="pt")

    # Instantiate dataloaders.
    train_dataloader = DataLoader(
        tokenized_datasets["train"], shuffle=True, collate_fn=collate_fn, batch_size=batch_size
    )
    eval_dataloader = DataLoader(
        tokenized_datasets["validation"], shuffle=False, collate_fn=collate_fn, batch_size=EVAL_BATCH_SIZE
    )

    return train_dataloader, eval_dataloader


# For testing only
if os.environ.get("TESTING_MOCKED_DATALOADERS", None) == "1":
    from accelerate.test_utils.training import mocked_dataloaders

    get_dataloaders = mocked_dataloaders  # noqa: F811


def training_function(config, args):
    # Initialize accelerator
    accelerator = Accelerator(cpu=args.cpu, mixed_precision=args.mixed_precision)
    # Sample hyper-parameters for learning rate, batch size, seed and a few other HPs
    lr = config["lr"]
    num_epochs = int(config["num_epochs"])
    seed = int(config["seed"])
    batch_size = int(config["batch_size"])

    # New Code #
    # Parse out whether we are saving every epoch or after a certain number of batches
    if hasattr(args.checkpointing_steps, "isdigit"):
        if args.checkpointing_steps == "epoch":
            checkpointing_steps = args.checkpointing_steps
        elif args.checkpointing_steps.isdigit():
            checkpointing_steps = int(args.checkpointing_steps)
        else:
            raise ValueError(
                f"Argument `checkpointing_steps` must be either a number or `epoch`. `{args.checkpointing_steps}` passed."
            )
    else:
        checkpointing_steps = None

    set_seed(seed)

    train_dataloader, eval_dataloader = get_dataloaders(accelerator, batch_size)
    metric = evaluate.load("glue", "mrpc")

    # If the batch size is too big we use gradient accumulation
    gradient_accumulation_steps = 1
    if batch_size > MAX_GPU_BATCH_SIZE and accelerator.distributed_type != DistributedType.TPU:
        gradient_accumulation_steps = batch_size // MAX_GPU_BATCH_SIZE
        batch_size = MAX_GPU_BATCH_SIZE

    # Instantiate the model (we build the model here so that the seed also control new weights initialization)
    model = AutoModelForSequenceClassification.from_pretrained("bert-base-cased", return_dict=True)

    # We could avoid this line since the accelerator is set with `device_placement=True` (default value).
    # Note that if you are placing tensors on devices manually, this line absolutely needs to be before the optimizer
    # creation otherwise training will not work on TPU (`accelerate` will kindly throw an error to make us aware of that).
    model = model.to(accelerator.device)

    # Instantiate optimizer
    optimizer = AdamW(params=model.parameters(), lr=lr)

    # Instantiate scheduler
    lr_scheduler = get_linear_schedule_with_warmup(
        optimizer=optimizer,
        num_warmup_steps=100,
        num_training_steps=(len(train_dataloader) * num_epochs) // gradient_accumulation_steps,
    )

    # Prepare everything
    # There is no specific order to remember, we just need to unpack the objects in the same order we gave them to the
    # prepare method.
    model, optimizer, train_dataloader, eval_dataloader, lr_scheduler = accelerator.prepare(
        model, optimizer, train_dataloader, eval_dataloader, lr_scheduler
    )

    # New Code #
    # We need to keep track of how many total steps we have iterated over
    overall_step = 0
    # We also need to keep track of the stating epoch so files are named properly
    starting_epoch = 0

    # We need to load the checkpoint back in before training here with `load_state`
    # The total number of epochs is adjusted based on where the state is being loaded from,
    # as we assume continuation of the same training script
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]

        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
        else:
            resume_step = int(training_difference.replace("step_", ""))
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)

    # Now we train the model
    for epoch in range(starting_epoch, num_epochs):
        model.train()
        for step, batch in enumerate(train_dataloader):
            # New Code #
            # We need to skip steps until we reach the resumed step during the first epoch
            if args.resume_from_checkpoint and epoch == starting_epoch:
                if resume_step is not None and step < resume_step:
                    overall_step += 1
                    continue
            # We could avoid this line since we set the accelerator with `device_placement=True`.
            batch.to(accelerator.device)
            outputs = model(**batch)
            loss = outputs.loss
            loss = loss / gradient_accumulation_steps
            accelerator.backward(loss)
            if step % gradient_accumulation_steps == 0:
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()
            # New Code #
            overall_step += 1

            # New Code #
            # We save the model, optimizer, lr_scheduler, and seed states by calling `save_state`
            # These are saved to folders named `step_{overall_step}`
            # Will contain files: "pytorch_model.bin", "optimizer.bin", "scheduler.bin", and "random_states.pkl"
            # If mixed precision was used, will also save a "scalar.bin" file
            if isinstance(checkpointing_steps, int):
                output_dir = f"step_{overall_step}"
                if overall_step % checkpointing_steps == 0:
                    if args.output_dir is not None:
                        output_dir = os.path.join(args.output_dir, output_dir)
                    accelerator.save_state(output_dir)

        model.eval()
        for step, batch in enumerate(eval_dataloader):
            # We could avoid this line since we set the accelerator with `device_placement=True` (the default).
            batch.to(accelerator.device)
            with torch.no_grad():
                outputs = model(**batch)
            predictions = outputs.logits.argmax(dim=-1)
            # It is slightly faster to call this once, than multiple times
            predictions, references = accelerator.gather_for_metrics((predictions, batch["labels"]), eval_dataloader)
            metric.add_batch(
                predictions=predictions,
                references=references,
            )

        eval_metric = metric.compute()
        # Use accelerator.print to print only on the main process.
        accelerator.print(f"epoch {epoch}:", eval_metric)

        # New Code #
        # We save the model, optimizer, lr_scheduler, and seed states by calling `save_state`
        # These are saved to folders named `epoch_{epoch}`
        # Will contain files: "pytorch_model.bin", "optimizer.bin", "scheduler.bin", and "random_states.pkl"
        # If mixed precision was used, will also save a "scalar.bin" file
        if checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            accelerator.save_state(output_dir)


def main():
    parser = argparse.ArgumentParser(description="Simple example of training script.")
    parser.add_argument(
        "--mixed_precision",
        type=str,
        default="no",
        choices=["no", "fp16", "bf16"],
        help="Whether to use mixed precision. Choose"
        "between fp16 and bf16 (bfloat16). Bf16 requires PyTorch >= 1.10."
        "and an Nvidia Ampere GPU.",
    )
    parser.add_argument("--cpu", action="store_true", help="If passed, will train on the CPU.")
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--output_dir",
        type=str,
        default=".",
        help="Optional save directory where all checkpoint folders will be stored. Default is the current working directory.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    args = parser.parse_args()
    config = {"lr": 2e-5, "num_epochs": 3, "seed": 42, "batch_size": 16}
    training_function(config, args)


if __name__ == "__main__":
    main()
