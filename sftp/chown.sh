#!/bin/sh
# atmoz/sftp runs scripts in /etc/sftp.d/ as root at startup, before dropping
# privileges. Ensure the (mounted) upload dir is owned by the sftp user so
# uploads succeed.
chown -R 1001:1001 /home/pharma/upload
