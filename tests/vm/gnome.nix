# A throwaway NixOS VM with a GNOME (Wayland) session, for trying AirMedia Share against
# GNOME's screen-sharing portal (xdg-desktop-portal-gnome on mutter) instead of KDE's.
#
# It does not touch your own system configuration. Build and start it from the repo:
#
#   nix-build '<nixpkgs/nixos>' -A vm -I nixos-config=$PWD/tests/vm/gnome.nix -o /tmp/airmedia-vm
#   NIX_DISK_IMAGE=/tmp/airmedia-vm.qcow2 /tmp/airmedia-vm/bin/run-airmedia-gnome-vm-vm
#
# The VM signs in as "tester" (password "tester") and shares this repo as /repo.
# Set AIRMEDIA_VM_SSH_PUBKEY to a public key when building to log in over ssh without a
# password: `ssh -p 2222 tester@localhost` (the VM forwards host port 2222 to its ssh).
{ config, lib, pkgs, ... }:

let
  repo = toString ../..;
  # The same packages `nix-shell` gives you. Having them in the VM's own closure means
  # `nix-shell /repo/shell.nix` works inside the VM without downloading anything.
  shell = import ../../shell.nix { inherit pkgs; };
  pubkey = builtins.getEnv "AIRMEDIA_VM_SSH_PUBKEY";
  # A quiet tone from ONE long-lived paplay process, so "Sound from" has an app to list.
  # (A loop of short plays would change process id every few seconds, which an app
  # picked by process id could never follow.)
  tone = pkgs.writeShellScript "tone" ''
    ${pkgs.sox}/bin/sox -q -n -r 48000 -b 16 -c 2 -e signed-integer -t raw - \
        synth 86400 sine 440 vol 0.2 \
      | ${pkgs.pulseaudio}/bin/paplay --raw --rate=48000 --channels=2 --format=s16le
  '';
in
{
  system.stateVersion = "26.05";
  networking.hostName = "airmedia-gnome-vm";

  # ----- GNOME on Wayland, signed in automatically
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;
  services.displayManager.autoLogin = { enable = true; user = "tester"; };
  # Autologin needs these off, or GNOME is started on a console that is already taken.
  systemd.services."getty@tty1".enable = false;
  systemd.services."autovt@tty1".enable = false;
  environment.gnome.excludePackages = [ pkgs.gnome-initial-setup ];

  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };
  security.rtkit.enable = true;

  # No lock screen or blanking while a test waits for a person, and no welcome tour.
  programs.dconf.profiles.user.databases = [{
    settings = {
      "org/gnome/desktop/session".idle-delay = lib.gvariant.mkUint32 0;
      "org/gnome/desktop/screensaver".lock-enabled = false;
      "org/gnome/desktop/lockdown".disable-lock-screen = true;
      "org/gnome/shell".welcome-dialog-last-shown-version = "999";
    };
  }];

  # ----- the user, and a way in from the host
  users.users.tester = {
    isNormalUser = true;
    initialPassword = "tester";
    extraGroups = [ "wheel" "video" ];
    openssh.authorizedKeys.keys = lib.optional (pubkey != "") pubkey;
  };
  security.sudo.wheelNeedsPassword = false;
  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = true;
  };

  # ----- what AirMedia Share needs, and something to hear
  environment.systemPackages = shell.buildInputs ++ shell.nativeBuildInputs;
  nix.nixPath = [ "nixpkgs=${pkgs.path}" ];
  # /repo is your real checkout: keep Python from leaving __pycache__ folders in it.
  environment.variables.PYTHONDONTWRITEBYTECODE = "1";

  systemd.user.services.tone = {
    description = "A quiet looping tone, so an app is playing sound";
    wantedBy = [ "graphical-session.target" ];
    after = [ "pipewire-pulse.service" ];
    serviceConfig = {
      ExecStart = "${tone}";
      Restart = "always";
      RestartSec = 2;
    };
  };

  # ----- VM-only settings
  virtualisation.vmVariant.virtualisation = {
    memorySize = 4096;
    cores = 4;
    diskSize = 8192;
    resolution = { x = 1280; y = 800; };
    sharedDirectories.repo = { source = repo; target = "/repo"; };
    forwardPorts = [{ from = "host"; host.port = 2222; guest.port = 22; }];
  };
}
