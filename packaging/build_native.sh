#!/usr/bin/env bash
# Build the redistributable LGPL runtime on macOS or MSYS2 UCRT64 Windows.
set -eo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
VERSION=8.0.3
SHA=6136812ea6d4e68bdba27e33c2a94382711cdf4f8602ffef056ff792bd6f9818
STAGE="$ROOT/build/native-runtime"
SOURCE="$ROOT/build/ffmpeg-$VERSION.tar.xz"
mkdir -p "$ROOT/build" "$STAGE"
if [ ! -f "$SOURCE" ]; then
  curl --fail --location --retry 3 "https://ffmpeg.org/releases/ffmpeg-$VERSION.tar.xz" -o "$SOURCE"
fi
if command -v shasum >/dev/null; then
  ACTUAL=$(shasum -a 256 "$SOURCE" | awk '{print $1}')
else
  ACTUAL=$(sha256sum "$SOURCE" | awk '{print $1}')
fi
[ "$ACTUAL" = "$SHA" ] || { echo 'FFmpeg source checksum mismatch'; exit 1; }
tar -xf "$SOURCE" -C "$ROOT/build"
cd "$ROOT/build/ffmpeg-$VERSION"
PLATFORM=()
SUFFIX=""
LIBSUFFIX=dylib
SHARED=(-dynamiclib -Wl,-install_name,@rpath/overview-dsp.dylib)
UNICODE=()
if [[ "$(uname -s)" == MINGW* || "$(uname -s)" == MSYS* ]]; then
  PLATFORM=(--target-os=mingw32 --cc=gcc --extra-ldflags=-static-libgcc)
  SUFFIX=.exe
  LIBSUFFIX=dll
  SHARED=(-shared -Wl,--export-all-symbols -static-libgcc)
  UNICODE=(-municode -static-libgcc)
fi
# No GPL/nonfree components, network protocols, hardware acceleration or external codecs.
./configure --prefix="$STAGE/sdk" --disable-autodetect --disable-gpl --disable-nonfree \
  --disable-doc --disable-debug --disable-network --disable-everything \
  --enable-shared --disable-static --enable-ffmpeg --enable-ffprobe \
  --enable-avcodec --enable-avformat --enable-avutil --enable-avfilter --enable-swresample \
  --disable-avdevice --disable-swscale \
  --enable-protocol=file \
  --enable-demuxer=wav,aiff,mp3,mov,flac,aac,ogg \
  --enable-decoder=pcm_s16le,pcm_s16be,pcm_s24le,pcm_s24be,pcm_s32le,pcm_s32be,pcm_f32le,pcm_f32be,pcm_f64le,pcm_f64be,mp3,mp3float,aac,flac,alac,vorbis,opus \
  --enable-parser=aac,mpegaudio,flac,opus,vorbis \
  --enable-encoder=pcm_f32le,pcm_s16le,aac,flac \
  --enable-muxer=pcm_f32le,wav,ipod,adts,flac \
  --enable-filter=aresample,aformat,anull,abuffer,abuffersink \
  "${PLATFORM[@]}"
make -j "${NATIVE_JOBS:-4}"
make install
cp "$STAGE/sdk/bin/ffmpeg$SUFFIX" "$STAGE/sdk/bin/ffprobe$SUFFIX" "$STAGE/"
if [ "$LIBSUFFIX" = dylib ]; then
  cp -L "$STAGE"/sdk/lib/lib*.dylib "$STAGE/"
else
  cp "$STAGE"/sdk/bin/*.dll "$STAGE/"
  # GCC's exception-bearing support runtimes, when used by this toolchain.
  for dll in libgcc_s_seh-1.dll libwinpthread-1.dll; do
    if [ -f "/ucrt64/bin/$dll" ]; then cp "/ucrt64/bin/$dll" "$STAGE/"; fi
  done
fi
cc -O2 -ffp-contract=off "${SHARED[@]}" "$ROOT/src/rb2engine/overviews/dsp.c" -o "$STAGE/overview-dsp.$LIBSUFFIX" -lm
cc -O2 "${UNICODE[@]}" "$ROOT/src/rb2engine/overviews/aac_decode.c" \
  -I"$STAGE/sdk/include" -L"$STAGE/sdk/lib" -lavformat -lavcodec -lavutil -o "$STAGE/aac-reader$SUFFIX"
if [ "$LIBSUFFIX" = dylib ]; then
  # Every executable and dylib resolves the replaceable LGPL libraries beside itself.
  for binary in "$STAGE/ffmpeg" "$STAGE/ffprobe" "$STAGE/aac-reader" "$STAGE"/*.dylib; do
    while IFS= read -r dependency; do
      install_name_tool -change "$dependency" "@loader_path/$(basename "$dependency")" "$binary"
    done < <(otool -L "$binary" | awk '/\/sdk\/lib\// {print $1}')
    if [[ "$binary" == *.dylib ]]; then install_name_tool -id "@loader_path/$(basename "$binary")" "$binary"; fi
    codesign --force --sign - "$binary"
  done
fi
cp config.h ffbuild/config.mak "$STAGE/"
cp COPYING.LGPLv2.1 LICENSE.md "$STAGE/"
"$STAGE/ffmpeg$SUFFIX" -version
