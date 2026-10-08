{
  description = "herdr — terminal workspace manager for AI coding agents";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    rust-overlay = {
      url = "github:oxalica/rust-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      rust-overlay,
    }:
    let
      lib = nixpkgs.lib;
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = lib.genAttrs systems;
      pkgsFor =
        system:
        import nixpkgs {
          inherit system;
          overlays = [ rust-overlay.overlays.default ];
        };
      rustToolchainFor = pkgs: pkgs.rust-bin.fromRustupToolchainFile ./rust-toolchain.toml;
      rustDevToolchainFor =
        pkgs:
        (rustToolchainFor pkgs).override (toolchain: {
          extensions = toolchain.extensions ++ [
            "rust-src"
            "rust-analyzer"
          ];
        });
      rustPlatformFor =
        pkgs:
        let
          rustToolchain = rustToolchainFor pkgs;
        in
        pkgs.makeRustPlatform {
          cargo = rustToolchain;
          rustc = rustToolchain;
        };
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          herdr = pkgs.callPackage ./nix/package.nix {
            rustPlatform = rustPlatformFor pkgs;
          };
        in
        {
          inherit herdr;
          default = herdr;
        }
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/herdr";
          meta.description = "Run Herdr";
        };
      });

      checks = forAllSystems (system: {
        herdr = self.packages.${system}.default;
        default = self.checks.${system}.herdr;
      });

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          rustToolchain = rustDevToolchainFor pkgs;
        in
        {
          default = pkgs.mkShell {
            name = "herdr-dev";
            packages =
              with pkgs;
              [
                cargo-nextest
                cmake
                just
                ninja
                pkg-config
                # On macOS /usr/bin/python3 is an xcrun shim, which the
                # xcbuild xcrun below cannot resolve.
                python3
                rustToolchain
                zig_0_16
              ]
              # The vendored libghostty-vt build runs nmedit through xcrun.
              # Inside this shell xcrun searches the Nix SDK rather than the
              # system tools, so it needs the same toolchain the package uses.
              ++ lib.optionals stdenv.hostPlatform.isDarwin [
                cctools
                xcbuild
              ];

            env = {
              LIBGHOSTTY_VT_OPTIMIZE = "Debug";
              LIBGHOSTTY_VT_SIMD = "true";
            };
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt);

      overlays.default = lib.composeExtensions rust-overlay.overlays.default (
        final: _prev: {
          herdr = final.callPackage ./nix/package.nix {
            rustPlatform = rustPlatformFor final;
          };
        }
      );
    };
}
