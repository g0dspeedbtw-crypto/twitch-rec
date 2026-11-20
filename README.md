# Twitch Recorder Pro — Studio Suite

Нативное десктопное приложение для **Windows** и **macOS** для одновременной записи прямых эфиров (Twitch, YouTube Live, Kick, VK Play Live, Trovo), скачивания VOD и клипов, визуального блюра рекламных баннеров и монтажа с AI.

## Запуск на Windows (`.exe`)
- Запустите **`dist\TwitchRecorderPro\TwitchRecorderPro.exe`** (или ярлык **`TwitchRecorderPro.lnk`** в корне проекта).
- Все зависимости (Python, Chromium UI, `FFmpeg`, `FFprobe`, `Streamlink`, `yt-dlp`, `OpenCV`, `Whisper AI`) вшиты внутрь сборки — приложение полностью автономно и запускается без `.bat` и без консольных окон.
- Пересборка `.exe`:
  ```powershell
  powershell -ExecutionPolicy Bypass -File .\build\build_windows.ps1
  ```

## Сборка и запуск на macOS (`.app` и `.dmg`)
Код полностью адаптирован под **Apple Silicon (M1–M4)** и Intel Mac:
- аппаратное ускорение **Apple VideoToolbox (`h264_videotoolbox`)**;
- нативные шрифты **SF Pro**, **Finder (`open -R`)**, **LaunchAgent** автозапуск;
- сохранение данных в `~/Library/Application Support/TwitchRecorderPro` и записей в `~/Movies/TwitchRecorderPro`.

### Вариант 1: Сборка на Mac одной командой
Скопируйте папку проекта на Mac и выполните в Терминале:
```bash
chmod +x build/build_macos.sh
./build/build_macos.sh
```
На выходе получите готовое приложение **`dist/TwitchRecorderPro.app`** и установочный образ **`dist/TwitchRecorderPro-macOS.dmg`**.

### Вариант 2: Автосборка и облачные обновления через GitHub Releases
В проект встроен полноценный механизм **облачных обновлений** (Windows и macOS) на базе **GitHub Releases**:
1. Выложите проект на GitHub (`git init && git remote add origin https://github.com/<user>/<repo>.git`).
2. Для выпуска новой версии выполните одну команду:
   ```bash
   ./build/publish_release.sh 2.1.1 "Описание изменений"
   ```
3. Workflow [`.github/workflows/build-releases.yml`](.github/workflows/build-releases.yml) автоматически соберёт сборки для **Windows** (`.zip`) и **macOS** (`.dmg` + `.zip`), вошьёт адрес вашего репозитория внутрь сборок и опубликует **GitHub Release**.
4. На всех остальных устройствах приложение само увидит новую версию при запуске (или по кнопке **Настройки → Система и Трей → Проверить**), скачает обновление и установит его в 1 клик с сохранением `config.json` и записей.
