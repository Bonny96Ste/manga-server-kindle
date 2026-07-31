MangaBridge v1.0.5 - Kindle 4 / KUAL / KOReader

This package is USB-installed. Extract it into the Kindle USB root.
It directly places:

  /mnt/us/extensions/mangabridge
  /mnt/us/koreader/plugins/mangabridge.koplugin
  /mnt/us/mangabridge

The KUAL Repair action is optional; the files are already installed by extraction.

After extraction:
1. Edit /mnt/us/mangabridge/config.lua over USB.
2. Safely eject the Kindle.
3. Reopen KUAL.
4. Choose MangaBridge -> Launch MangaBridge.

If launch fails, use Write diagnostics report and read:
  /mnt/us/mangabridge/diagnostics.txt

This extension does not modify the Kindle system partition.
