# Use a graphics card in a sandbox

Some work needs a real graphics card: running a model on your own machine, training one, or converting video quickly. SmolVM can lend a sandbox one of the graphics cards in your computer, so the code inside gets the actual hardware rather than a slow imitation.

The card is lent, not shared. While the sandbox is running, that card belongs to it — your computer and any other sandbox cannot use it. When you stop the sandbox, you get it back.

This needs a Linux computer with a graphics card you can spare. It is not available on a Mac.

## See what you have

Start here. This command lists the graphics cards in your computer and tells you whether each one is ready to lend:

```bash
smolvm gpu list
```

```text
                 Graphics cards on this machine
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━┓
┃ Address      ┃ Card        ┃ Ids       ┃ Used by ┃ Available ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━┩
│ 0000:01:00.0 │ NVIDIA 2684 │ 10de:2684 │ nvidia  │ no        │
└──────────────┴─────────────┴───────────┴─────────┴───────────┘
```

**Address** is how you name a card in later commands. **Used by** is what currently has the card: `vfio-pci` means it is free for sandboxes, and anything else means your computer is still using it.

If a card is not available yet, the command prints the one-time setup steps for your computer. Those steps need administrator access and a restart, and they change your computer rather than any sandbox. SmolVM never does them for you — freeing the wrong card would take your screen away in the middle of your work.

## Lend a card to a sandbox

Once `smolvm gpu list` shows a card as available:

```bash
smolvm sandbox create --name train --gpu 0000:01:00.0
```

If exactly one card is free, you can skip looking up the address:

```bash
smolvm sandbox create --name train --gpu auto
```

Check that the sandbox can see it:

```bash
smolvm sandbox exec train -- lspci
```

## Install the card's driver

Seeing the card is not the same as being able to use it. The code inside the sandbox needs the manufacturer's driver, and that driver has to be installed inside the sandbox like any other software:

```bash
smolvm sandbox shell train

# inside the sandbox
apt-get update
apt-get install -y build-essential
apt-get install -y nvidia-driver-550    # match your card
nvidia-smi
```

SmolVM does not ship the driver itself. Graphics drivers come with their own licences and are tied to specific card models, so which one you need is your choice.

## Lend more than one card

Repeat the option:

```bash
smolvm sandbox create --name train --gpu 0000:01:00.0 --gpu 0000:02:00.0
```

## Things to know

**A card usually comes with a sound part.** Graphics cards have a sound output built into the same chip, and your computer treats the two as one unit that can only be lent together. SmolVM works this out for you — you name the card, and it hands over everything that belongs with it. This is also why `smolvm gpu list` sometimes says a card is held back by a different address than the one you asked about.

**Saving a sandbox's memory does not work with a card.** Saving memory means recording the state of every piece of hardware, and a real graphics card cannot be recorded that way. Saving just the disk works normally:

```bash
smolvm sandbox snapshot create train --snapshot-type disk
```

**Your computer may limit reserved memory.** A sandbox using a graphics card has to keep all its memory reserved. If your computer's limit is lower than the sandbox size, SmolVM says so before starting. `ulimit -l unlimited` lifts it for the current terminal when your account is allowed to go that high; if it answers "Operation not permitted", add `* - memlock unlimited` to `/etc/security/limits.conf` and log in again.

**Sandboxes with a card start a little slower.** They use a more compatible virtual machine layout, because the faster one SmolVM normally picks has nowhere to plug a card in.

## Implementation notes

Card discovery reads `/sys` only and never changes driver bindings — see [`src/smolvm/host/gpu.py`](../../src/smolvm/host/gpu.py). The sandbox setting is `VMConfig.gpus` in [`src/smolvm/types.py`](../../src/smolvm/types.py), which also holds the rules about which sandboxes can use a card. The QEMU command line is assembled in [`src/smolvm/runtime/qemu_args.py`](../../src/smolvm/runtime/qemu_args.py), and the start-time and snapshot guards live in [`src/smolvm/vm.py`](../../src/smolvm/vm.py). Guest driver support comes from the `gpu` kernel variant described in [`kernel/microvm/README.md`](../../kernel/microvm/README.md). Behavior is covered by [`tests/test_gpu.py`](../../tests/test_gpu.py), [`tests/test_qemu_args.py`](../../tests/test_qemu_args.py), [`tests/test_types.py`](../../tests/test_types.py), and [`tests/test_kernel_config.py`](../../tests/test_kernel_config.py).
