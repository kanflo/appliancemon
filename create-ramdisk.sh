#!/bin/bash
#
# Create 2Mb RAM-disk in /ram and add to fstab if missing
#
# Must be run as root

mkdir -p /ram
chown pi:pi /ram
grep tmpfs /etc/fstab || echo "tmpfs /ram tmpfs nodev,nosuid,size=2M 0 0" >> /etc/fstab
mount -a
df -h | grep ram
