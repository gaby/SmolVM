# SmolVM

## Install

```bash
pip install SmolVM
```

## Use

```python
from smolvm import SmolVM

vm = SmolVM(name="demo", vcpus=1, memory_mib=512)
vm.start()
print(vm.status())
vm.stop()
```
