# Build image for C and C++ repositories: compilers, build systems, and the
# development libraries the benchmark repositories need.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Types: deb$/Types: deb deb-src/' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential clang cmake ninja-build meson scons pkg-config \
        nasm yasm gettext unzip curl ca-certificates git file \
        python3 python3-pip python3-zstandard perl tcl \
        libcurl4-openssl-dev libexpat1-dev libssl-dev zlib1g-dev libpcre2-dev \
        libsdl2-dev libavcodec-dev libavdevice-dev libavformat-dev \
        libavutil-dev libswresample-dev libusb-1.0-0-dev \
        libx11-dev libxcursor-dev libxinerama-dev libgl1-mesa-dev \
        libglu1-mesa-dev libasound2-dev libpulse-dev libudev-dev libxi-dev \
        libxrandr-dev libwayland-dev libdbus-1-dev libspeechd-dev \
        libxkbcommon-dev libfontconfig1-dev libfreetype-dev \
        extra-cmake-modules qt6-base-dev qt6-base-private-dev qt6-svg-dev \
    && apt-get build-dep -y obs-studio \
    && rm -rf /var/lib/apt/lists/*
# git's meson build enables its Rust components by default. Install the
# toolchain in world-readable folders because containers run as the host user.
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path \
    && chmod -R a+rX /usr/local/rustup /usr/local/cargo
# scrcpy needs SDL3, which Ubuntu 24.04 does not package.
RUN apt-get update && apt-get install -y --no-install-recommends libxtst-dev libxss-dev luajit lua5.1 \
    && rm -rf /var/lib/apt/lists/*
RUN curl -sSL https://github.com/libsdl-org/SDL/releases/download/release-3.4.20/SDL3-3.4.20.tar.gz \
        | tar -xz -C /tmp \
    && cmake -S /tmp/SDL3-3.4.20 -B /tmp/sdl3-build -DCMAKE_BUILD_TYPE=Release -DSDL_TESTS=OFF \
    && cmake --build /tmp/sdl3-build -j"$(nproc)" \
    && cmake --install /tmp/sdl3-build --prefix /usr/local \
    && rm -rf /tmp/SDL3-3.4.20 /tmp/sdl3-build \
    && ldconfig
# scrcpy builds its Android server with gradle, which needs a JDK and the
# Android SDK. Gradle itself is downloaded by the repository's wrapper.
ENV ANDROID_HOME=/opt/android-sdk ANDROID_SDK_ROOT=/opt/android-sdk \
    PATH=/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-21-jdk-headless \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/android-sdk/cmdline-tools \
    && curl -sSL -o /tmp/cmdline-tools.zip \
        https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip \
    && unzip -q /tmp/cmdline-tools.zip -d /opt/android-sdk/cmdline-tools \
    && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest \
    && rm /tmp/cmdline-tools.zip \
    && yes | sdkmanager --sdk_root=/opt/android-sdk --licenses > /dev/null \
    && sdkmanager --sdk_root=/opt/android-sdk "platforms;android-36" "build-tools;36.0.0" "platform-tools" \
    && chmod -R a+rX /opt/android-sdk
