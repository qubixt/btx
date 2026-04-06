# Windows 11 Build and Mining Guide

This is the BTX-specific Windows path. It replaces the older upstream-style
Bitcoin Core instructions that assumed a different dependency mix and a simpler
mining/runtime story.

If you are new to BTX on Windows, start with the step-by-step handbook:
[btx-windows-11-compile-handbook.md](./btx-windows-11-compile-handbook.md).

The wrapper and steps in this guide define the intended public Windows path.
If the current public branch still has unresolved MSVC portability issues, the
wrapper will stop at the first compiler error so you can see the exact failure.

The recommended target on Windows is a headless build that produces:

- `btxd.exe`
- `btx-cli.exe`

That path is lighter, avoids unnecessary Qt failures, and is the cleanest way
to go from clone to a wallet-capable BTX node and first solo-mining run.

For MinGW or cross-build workflows, see [build-windows.md](./build-windows.md).

## What We Learned on a Real Windows 11 Build

These are the practical lessons baked into the wrapper:

- use **Visual Studio 2022 Build Tools** or **Visual Studio 2022 Community**
  with the Desktop C++ workload
- use a **standalone vcpkg checkout on a short path** such as
  `C:\btx-deps\vcpkg`, not the default Visual Studio-managed path
- keep `VCPKG_INSTALLED_DIR` and `vcpkg` buildtrees on **short paths without
  spaces**
- default to **`BUILD_GUI=OFF`** unless you explicitly need Qt
- build a **wallet-enabled** node by default so users can actually mine to a
  local wallet after sync
- treat CUDA as **optional**, not the default public Windows dependency

## Dependencies

### Required

- Windows 11 x64
- Git for Windows
- Visual Studio 2022 Build Tools or Community
  - Desktop development with C++
- CMake

### Recommended

- Python 3.11
  - needed for the broader test/tooling path
- enough free disk space for:
  - the source tree
  - a vcpkg checkout
  - the build directory
  - the BTX datadir

### Optional

- NVIDIA CUDA Toolkit
  - only if you want to experiment with the BTX CUDA backend
- Nsight Compute
  - useful for GPU profiling

Known-good `winget` package IDs:

- `Git.Git`
- `Microsoft.VisualStudio.2022.BuildTools`
- `Kitware.CMake`
- `Python.Python.3.11`
- `Nvidia.CUDA`
- `Nvidia.Nsight.Compute`

## Fast Path: Clone to Working Binaries

Clone the repository:

```powershell
git clone https://github.com/btxchain/btx.git
cd .\btx
```

Run the Windows build wrapper:

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1 `
  -InstallDependencies
```

What the wrapper does:

- checks for Git, CMake, and VS 2022 C++ build tools
- installs missing dependencies with `winget` when requested
- clones and bootstraps a standalone `vcpkg` to `C:\btx-deps\vcpkg`
- keeps vcpkg state on short paths to avoid common Windows path-length issues
- configures a wallet-enabled headless BTX build
- builds the binaries
- after a successful compile, runs a regtest smoke test that boots `btxd`,
  creates a wallet, and mines one block

Default output directory:

```text
.\build-windows-msvc\bin\Release\
```

Expected binaries:

```text
.\build-windows-msvc\bin\Release\btxd.exe
.\build-windows-msvc\bin\Release\btx-cli.exe
```

## Useful Wrapper Variants

### Build benches too

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1 `
  -InstallDependencies `
  -BuildBench
```

### Build unit-test targets too

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1 `
  -InstallDependencies `
  -BuildTests
```

### Enable the experimental CUDA backend

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1 `
  -InstallDependencies `
  -InstallCudaToolkit `
  -EnableCudaExperimental `
  -BuildBench
```

After a CUDA build, inspect backend selection with:

```powershell
.\build-windows-msvc\bin\Release\btx-matmul-backend-info.exe --backend cuda
```

## Manual Dependency Install

If you prefer to install prerequisites yourself:

```powershell
winget install --id Git.Git --exact --source winget
winget install --id Kitware.CMake --exact --source winget
winget install --id Python.Python.3.11 --exact --source winget
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --source winget `
  --override "--quiet --wait --norestart --nocache --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

Optional CUDA tooling:

```powershell
winget install --id Nvidia.CUDA --exact --source winget
winget install --id Nvidia.Nsight.Compute --exact --source winget
```

Then run the wrapper without dependency installation:

```powershell
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1
```

## From Build to Running Node

Create a minimal config at `%APPDATA%\BTX\btx.conf`:

```ini
server=1
listen=1
txindex=1
```

Start the node in a new process:

```powershell
Start-Process -FilePath .\build-windows-msvc\bin\Release\btxd.exe
```

Watch sync:

```powershell
.\build-windows-msvc\bin\Release\btx-cli.exe getblockchaininfo
.\build-windows-msvc\bin\Release\btx-cli.exe getmininginfo
```

BTX mining should wait until:

- `initialblockdownload` is `false`
- `getmininginfo.chain_guard.should_pause_mining` is `false`
- the node is effectively at tip

## Create a Wallet for Mining

Create a descriptor wallet and fetch a reward address:

```powershell
.\build-windows-msvc\bin\Release\btx-cli.exe createwallet reapermining
.\build-windows-msvc\bin\Release\btx-cli.exe -rpcwallet=reapermining getnewaddress
```

Back it up after you fund it or after you encrypt it:

```powershell
.\build-windows-msvc\bin\Release\btx-cli.exe -rpcwallet=reapermining backupwallet "$HOME\\Desktop\\reapermining-backup.dat"
```

## Solo Mining from Windows PowerShell

For production-scale mining, the canonical external path is still
`getblocktemplate` / `submitblock`.

If you intentionally want to drive local solo mining directly against your
Windows node with the built-in RPC solver, use a health-aware loop instead of a
blind infinite `generatetoaddress` loop.

Example:

```powershell
$Cli = Resolve-Path .\build-windows-msvc\bin\Release\btx-cli.exe
$Address = (& $Cli -rpcwallet=reapermining getnewaddress).Trim()

while ($true) {
    $info = & $Cli getmininginfo | ConvertFrom-Json
    if (($info.initialblockdownload -eq $false) -and ($info.chain_guard.should_pause_mining -eq $false)) {
        & $Cli -rpcwallet=reapermining generatetoaddress 1 $Address | Out-Null
    } else {
        Start-Sleep -Seconds 5
    }
}
```

Useful health checks while mining:

```powershell
.\build-windows-msvc\bin\Release\btx-cli.exe getmininginfo
.\build-windows-msvc\bin\Release\btx-cli.exe getnetworkinfo
.\build-windows-msvc\bin\Release\btx-cli.exe getpeerinfo
```

## Troubleshooting

### vcpkg path or buildtrees failures

Use the wrapper defaults. They intentionally keep:

- `VCPKG_ROOT` on `C:\btx-deps\vcpkg`
- `VCPKG_INSTALLED_DIR` on `C:\btx-deps\vcpkg_installed\btx`
- buildtrees on `C:\btx-deps\vcpkg-buildtrees`

This avoids the most common Windows path-length and embedded-space failures.

### Repository path has spaces

The wrapper works around most of this, but the cleanest path is still cloning
to something short such as:

```powershell
C:\src\btx
```

### GUI build failures

Start with the default headless build. Only add `-BuildGui` when you actually
need `btx-qt`.

### CUDA build fails

Make sure:

- the NVIDIA driver is installed
- the CUDA toolkit is installed
- `nvcc.exe` exists
- you passed `-EnableCudaExperimental`

You can verify toolkit visibility with:

```powershell
nvcc --version
nvidia-smi
```

## Summary

For most Windows 11 users, this is the shortest reliable path:

```powershell
git clone https://github.com/btxchain/btx.git
cd .\btx
powershell -ExecutionPolicy Bypass -File .\contrib\devtools\build-btx-windows.ps1 -InstallDependencies
Start-Process -FilePath .\build-windows-msvc\bin\Release\btxd.exe
```

Then create a wallet, wait for full sync, and only begin mining when
`getmininginfo.chain_guard` says the node is ready.
