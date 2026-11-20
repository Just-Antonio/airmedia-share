# Development shell for NixOS (or any Nix user):
#
#   nix-shell                                            # enter the shell
#   nix-shell --run "python -m unittest discover -s tests -t . -v"
#   nix-shell --run "PYTHONPATH=. python -m airmedia_share"
#
# install.sh targets Debian/Ubuntu (apt); this is the equivalent set of dependencies
# for running from a checkout. Versions come from nixpkgs, so they can differ slightly
# from the exact pins in requirements.txt.
{ pkgs ? import <nixpkgs> { } }:

let
  python = pkgs.python3.withPackages (ps: with ps; [
    aiortc
    av
    numpy
    websockets
    pygobject3
  ]);
in
pkgs.mkShell {
  # The gobject-introspection hook fills GI_TYPELIB_PATH from buildInputs, and the
  # GStreamer hook fills GST_PLUGIN_SYSTEM_PATH_1_0 (pipewire ships pipewiresrc).
  nativeBuildInputs = [ pkgs.gobject-introspection ];
  buildInputs = [
    python
    pkgs.gtk3
    pkgs.libnotify
    pkgs.libwnck
    pkgs.gdk-pixbuf
    pkgs.pango
    pkgs.harfbuzz
    pkgs.atk
    pkgs.glib
    pkgs.gst_all_1.gstreamer
    pkgs.gst_all_1.gst-plugins-base
    pkgs.pipewire
    pkgs.pulseaudio # parec and pactl (they talk to pipewire-pulse)
    pkgs.zenity
  ];
}
