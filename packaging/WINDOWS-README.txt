rb2engine desktop — Windows x64

1. Extract the WHOLE ZIP to a local folder before launching rb2engine.exe.
   Keep the _internal folder beside the exe. Python does not need to be installed.
2. Requires Windows 10/11 x64 and Microsoft Edge WebView2 Evergreen Runtime.
   If the app reports a missing runtime, run MicrosoftEdgeWebview2Setup.exe,
   which is the signed Microsoft installer included in this download.
   The bootstrapper needs internet access. Offline installer:
   https://developer.microsoft.com/microsoft-edge/webview2/
3. Select the USB root containing PIONEER (for example E:\), not PIONEER itself.
   USB SSDs reported as fixed drives are also listed. Refresh after reconnecting.
4. Finish the rekordbox export and close rekordbox and Engine DJ before converting.
5. Read library, confirm, convert, then verify. An existing Engine m.db is backed
   up under Engine Library\Database2\rb2engine-backups before a rebuild.
   This is a database snapshot, not a complete audio/library backup.
6. Startup log: %LOCALAPPDATA%\rb2engine\desktop.log

The app runs locally and does not upload your library. Chinese/English is
switchable in the upper right. Waveform generation is not part of this UI;
Windows waveform generation remains unsupported by the existing CLI backend.

This preview is not Authenticode signed. Windows may show an unknown-publisher
or SmartScreen warning. Use only the archive from the project Actions/release
page and compare its SHA-256 with the accompanying file.

Automated Windows runner tests are not physical USB/player acceptance. Check
playlists, cues and actual playback on your equipment before using at a show.

中文：请完整解压压缩包，再打开 rb2engine.exe，勿单独移动 exe。
如提示缺少 WebView2，请运行同目录的微软官方安装程序（需要联网）。
选择含 PIONEER 的 U 盘根目录，完成导出并退出 DJ 软件后，再读取和转换。
程序不复制音乐、不修改 rekordbox 导出。已有 Engine 数据库会先保存副本。
