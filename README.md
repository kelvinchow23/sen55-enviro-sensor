# sen55-enviro-sensor

SEN5x (SEN50/54/55) I2C driver for Raspberry Pi, dockerized to run on a Pi Zero 2 W.

## Pi setup (one-time)

1. Enable I2C:
   ```
   sudo raspi-config nonint do_i2c 0
   sudo reboot
   ```
2. Install Docker, if not already present:
   ```
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   ```
   Log out/in (or `newgrp docker`) for the group change to take effect.

## Run

```
git clone https://github.com/kelvinchow23/sen55-enviro-sensor.git
cd sen55-enviro-sensor
docker compose up --build -d
```

View live sensor output:
```
docker compose logs -f
```

Stop:
```
docker compose down
```

The container is granted access to `/dev/i2c-1` only (no `--privileged`), and restarts automatically unless stopped.
