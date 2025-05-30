{
  inputs = {
    crate2nix = {
      url = "github:nix-community/crate2nix";
      inputs.nixpkgs.follows = "hf-nix/nixpkgs";
    };
    nix-filter.url = "github:numtide/nix-filter";
    hf-nix.url = "github:huggingface/hf-nix";
    nixpkgs.follows = "hf-nix/nixpkgs";
    flake-utils.url = "github:numtide/flake-utils";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "hf-nix/nixpkgs";
    };
  };
  outputs =
    {
      self,
      crate2nix,
      nix-filter,
      nixpkgs,
      flake-utils,
      rust-overlay,
      hf-nix,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        cargoNix = crate2nix.tools.${system}.appliedCargoNix {
          name = "tgi";
          src = ./.;
          additionalCargoNixArgs = [ "--all-features" ];
        };
        pkgs = import nixpkgs {
          inherit system;
          inherit (hf-nix.lib) config;
          overlays = [
            rust-overlay.overlays.default
            hf-nix.overlays.default
            (import nix/overlay.nix)
          ];
        };
        crateOverrides = import ./nix/crate-overrides.nix { inherit pkgs nix-filter; };
        benchmark = cargoNix.workspaceMembers.text-generation-benchmark.build.override {
          inherit crateOverrides;
        };
        launcher =
          let
            launcherUnwrapped = cargoNix.workspaceMembers.text-generation-launcher.build.override {
              inherit crateOverrides;
            };
            packagePath =
              with pkgs.python3.pkgs;
              makePythonPath [
                torch
              ];
          in
          pkgs.writeShellApplication {
            name = "text-generation-launcher";
            text = ''
              PYTHONPATH="${packagePath}" ${launcherUnwrapped}/bin/text-generation-launcher "$@"
            '';
          };

        router =
          let
            routerUnwrapped = cargoNix.workspaceMembers.text-generation-router-v3.build.override {
              inherit crateOverrides;
            };
            packagePath =
              with pkgs.python3.pkgs;
              makePythonPath [
                protobuf
                sentencepiece
                torch
                transformers
              ];
          in
          pkgs.writeShellApplication {
            name = "text-generation-router";
            text = ''
              PYTHONPATH="${packagePath}" ${routerUnwrapped}/bin/text-generation-router "$@"
            '';
          };
        server = pkgs.python3.pkgs.callPackage ./nix/server.nix { inherit nix-filter; };
        client = pkgs.python3.pkgs.callPackage ./nix/client.nix { };
      in
      {
        checks = {
          rust =
            with pkgs;
            rustPlatform.buildRustPackage {
              name = "rust-checks";
              src = ./.;
              cargoLock = {
                lockFile = ./Cargo.lock;
              };
              buildInputs = [ openssl.dev ];
              nativeBuildInputs = [
                clippy
                pkg-config
                protobuf
                python3
                rustfmt
              ];
              buildPhase = ''
                cargo check
              '';
              checkPhase = ''
                cargo fmt -- --check
                cargo test -j $NIX_BUILD_CORES
                cargo clippy
              '';
              installPhase = "touch $out";
            };
        };
        formatter = pkgs.nixfmt-rfc-style;
        devShells = with pkgs; rec {
          default = pure;

          pure = mkShell {
            buildInputs = [
              benchmark
              launcher
              router
              server
            ];
          };
          test = mkShell {
            buildInputs =
              [
                benchmark
                launcher
                router
                server
                client
                openssl.dev
                pkg-config
                cargo
                rustfmt
                clippy
              ]
              ++ (with python3.pkgs; [
                docker
                pytest
                pytest-asyncio
                syrupy
                pre-commit
                ruff
              ]);
          };

          impure = callPackage ./nix/impure-shell.nix { inherit server; };

          impureWithCuda = callPackage ./nix/impure-shell.nix {
            inherit server;
            withCuda = true;
          };

          impure-flash-attn-v1 = callPackage ./nix/impure-shell.nix {
            server = server.override { flash-attn = python3.pkgs.flash-attn-v1; };
          };
        };

        packages = rec {
          inherit server;

          default = pkgs.writeShellApplication {
            name = "text-generation-inference";
            runtimeInputs = [
              server
              router
            ];
            text = ''
              ${launcher}/bin/text-generation-launcher "$@"
            '';
          };

          # Use plain nixpkgs without overlays for dockerTools. dockerTools
          # uses a Python package for computing the layers from the transitive
          # closure. However, this needs a lot of rebuilds due to our overlay.

          dockerImage = nixpkgs.legacyPackages.${system}.callPackage nix/docker.nix {
            text-generation-inference = default;
          };

          dockerImageStreamed = nixpkgs.legacyPackages.${system}.callPackage nix/docker.nix {
            text-generation-inference = default;
            stream = true;
          };
        };
      }
    );
}
