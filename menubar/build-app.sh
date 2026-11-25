#!/bin/sh
# Builds menubar/build/AgenticCoach.app, unsigned, for use on this Mac.
# Run: sh menubar/build-app.sh
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/build/AgenticCoach.app"

# Normal path: Xcode is selected and the build uses its default SDK.
#
# Fallback for a Mac with only the Command Line Tools: the macOS 27 SDK cannot
# build SwiftUI there (it declares @State as a macro and the SwiftUIMacros
# compiler plugin ships only with Xcode), so the macOS 26 SDK that comes with
# the same tools is used unless SDKROOT is already set. Nothing on the Mac is
# changed; the variable lasts for this script only.
CLT_SDK="/Library/Developer/CommandLineTools/SDKs/MacOSX26.sdk"
if [ -z "${SDKROOT:-}" ] && [ "$(xcode-select -p)" = "/Library/Developer/CommandLineTools" ] && [ -d "$CLT_SDK" ]; then
    export SDKROOT="$CLT_SDK"
    echo "Using SDK $SDKROOT"
fi

swift build --package-path "$HERE" -c release
BIN="$(swift build --package-path "$HERE" -c release --show-bin-path)/AgenticCoach"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN" "$APP/Contents/MacOS/AgenticCoach"
cp "$HERE/Support/Info.plist" "$APP/Contents/Info.plist"

echo "Built $APP"
