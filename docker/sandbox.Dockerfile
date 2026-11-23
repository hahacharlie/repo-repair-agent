# Generic build sandbox for non-Python repositories. It holds compilers and
# the build systems that this tool supports (Cargo, Go, Meson, SCons, CMake,
# Make). It holds no project-specific libraries: the repair agents install
# those at run time, so each install is a recorded, reviewed repair step.
FROM ubuntu:26.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential clang cmake ninja-build meson scons pkg-config \
        autoconf automake libtool gettext bison flex nasm \
        git curl wget ca-certificates unzip zip xz-utils file patch \
        python3 python3-pip python3-venv golang-go \
    && rm -rf /var/lib/apt/lists/*
# Let go download the toolchain version that a go.mod file asks for.
ENV GOTOOLCHAIN=auto
# Containers run as the host user, so that user must be able to add the
# toolchain that a rust-toolchain file pins.
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs \
        | sh -s -- -y --profile minimal --default-toolchain stable --no-modify-path \
    && chmod -R a+rwX /usr/local/rustup /usr/local/cargo
