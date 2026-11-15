"""Tarn desktop: a native GTK3 front end for the laptop's own screen.

It talks to libvirt directly through the same Hypervisor and Sampler classes
the web server uses, so both front ends always agree. Runs under any Wayland
or X11 session, and is meant to run full screen in the cage kiosk compositor.

Needs py3-gobject3, py3-cairo and gtk+3.0 (apk), and the desktop user in the libvirt group.
"""
