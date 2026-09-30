# Rogue Renamer

**Rogue Renamer** is a free, open-source movie and TV file renamer for
Windows, built with Python and PySide6.

It identifies media using online metadata providers, previews proposed
filenames before making changes, and offers both a simple **Basic Mode**
and a more capable **Advanced Mode**.

## Features

-   Movie and TV episode identification and renaming
-   **Basic Mode** with a guided `Add → Match → Review → Rename`
    workflow
-   Basic Mode renames files in place and does not reorganize them
-   **Advanced Mode** for the complete Rogue Renamer toolset
-   Metadata support for TMDB, TheTVDB, OMDb, and AniList
-   Manual match review for ambiguous results
-   Batch renaming with preview and safety checks
-   Rename history and undo support
-   Read-only library auditing
-   Duplicate detection and inspection
-   Optional FFmpeg/ffprobe integration for deeper duplicate inspection
-   First-run setup wizard
-   User-supplied API credentials stored locally
-   Windows installer and standalone packaged application

## Basic Mode

Basic Mode is for users who simply want to rename media without managing
a complicated library workflow.

1.  **Add** movies, TV episodes, folders, or individual files.
2.  **Match** them against the selected metadata provider.
3.  **Review** uncertain matches.
4.  **Rename** the confirmed files.

Basic Mode renames media **in place**. It does not move files into a
different library structure. You can switch to Advanced Mode at any
time.

## Advanced Mode

Advanced Mode exposes Rogue Renamer's complete toolset, including
additional parsed metadata, library auditing, history/undo features,
organization options, and more detailed control over the rename process.

## Metadata Providers

Rogue Renamer supports **TMDB**, **TheTVDB**, **OMDb**, and **AniList**.

Rogue Renamer does **not** distribute shared API keys. Users obtain and
enter their own credentials through the first-run setup wizard or
Settings. AniList does not require the same API-key setup as the other
providers.

## FFmpeg / ffprobe

FFmpeg is optional.

Rogue Renamer can use `ffprobe`, included with FFmpeg, to inspect media
files when comparing duplicates. This can expose technical properties
such as resolution, codecs, and bitrate to help determine which copy may
be preferable to keep.

Rogue Renamer does **not automatically delete duplicate media**.
Duplicate findings are presented for review.

Install FFmpeg and ensure `ffprobe` is available if you want these
inspection features.

## Windows Installation

The recommended installation method is the Rogue Renamer installer from
the project's GitHub Releases page.

1.  Download `RogueRenamer-Setup-1.0.0.exe`.
2.  Run the installer.
3.  Launch Rogue Renamer from the Start Menu or optional desktop
    shortcut.
4.  Complete the first-run setup.
5.  Enter your own metadata-provider credentials.
6.  Choose Basic or Advanced Mode.

Python is **not required** when using the packaged Windows release.

## Running From Source

### Requirements

-   Python 3.11 or a compatible Python version
-   Dependencies listed in `requirements.txt`

Create a virtual environment and install the dependencies:

``` powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run Rogue Renamer:

``` powershell
python -m app.main
```

## Building the Windows Application

Rogue Renamer uses PyInstaller for the packaged Windows application.

``` powershell
python -m PyInstaller --noconfirm --clean --windowed --name "RogueRenamer" --icon "assets\rogue_renamer.ico" --version-file "version_info.txt" --add-data "assets;assets" app\main.py
```

The distributable application is created under:

``` text
dist\RogueRenamer\
```

The executable inside `build\` is an intermediate build artifact and
should not be distributed.

## Building the Windows Installer

`RogueRenamer.iss` contains the Inno Setup 6 installer definition.

Build the PyInstaller distribution first, then compile the Inno Setup
script. The resulting installer is written to the local `installer`
directory.

Build output such as `build/`, `dist/`, virtual environments, and
`installer/` should not be committed to the source repository.

## Configuration and Privacy

Rogue Renamer stores application configuration in the user's local
application-data directory rather than hard-coding personal API
credentials into the source.

API credentials entered into Rogue Renamer are intended to remain local
to that user's installation.

Do not commit API keys, tokens, passwords, `.env` files, or local
configuration files.

## File Safety

Rogue Renamer previews proposed changes before renaming files.

Library auditing and duplicate inspection are designed to identify
potential problems without automatically deleting duplicate media. Users
remain in control of destructive file-management decisions.

As with any bulk file-renaming utility, keeping a backup of important
media is recommended.

## Project Status

**Windows v1.0.0**

The Windows application and installer have been tested through the
complete installation and rename workflow.

Linux packaging is planned.

## Contributing

Issues, bug reports, and contributions are welcome.

When contributing:

-   Do not include personal API credentials or local configuration.
-   Keep Basic Mode approachable for new users.
-   Preserve preview and safety behavior around file operations.
-   Test file-renaming changes against disposable media.

## License

Rogue Renamer is released under the **MIT License**.

See [`LICENSE`](LICENSE) for the full license text.

------------------------------------------------------------------------

**Rogue Renamer**\
Rogue Systems
