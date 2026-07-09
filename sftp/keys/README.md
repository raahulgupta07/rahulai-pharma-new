# SFTP public keys

Drop authorized `*.pub` files here for key-based SFTP auth (mounted read-only
into the sftp container at /home/pharma/.ssh/keys). Password auth is also
enabled via `SFTP_PASSWORD` in `.env`.

Connect:
    sftp -P 2222 pharma@<host>            # password
    sftp -i <privkey> -P 2222 pharma@<host>   # key

Upload the two exports; the ingest worker auto-loads them within ~15s:
    put articles-export.xlsx
    put balance_stock.xlsx
