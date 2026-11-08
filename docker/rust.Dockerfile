# Build image for Rust repositories: the Rust toolchain plus the system
# libraries that native crates in the benchmark repositories link against.
FROM rust:bookworm
# rust-toolchain.toml files would make rustup download into the read-only
# image; use the image toolchain for every repository instead.
ENV RUSTUP_TOOLCHAIN=$RUST_VERSION
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        cmake pkg-config python3 libssl-dev \
        libfontconfig1-dev libfreetype-dev libxcb-xfixes0-dev libxkbcommon-dev \
        libsqlite3-dev libmariadb-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*
