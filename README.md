# sACN_PlaybackMachine

sACN_PlaybackMachine is a way to capture a single frame of multiple universes of DMX data over Streaming ACN (E1.31) 

The playback machine comes with a webserver for configuration of network, sACN, and GPIO.
It also features a control interface to capture and play scenes for testing.

## Hardware

This project was built on [WIZnets W5500-EVB-Pico](https://wiznet.io/products/evaluation-boards/w5500-evb-pico) The firmware file containing the build with the w5500 driver is added in the repository.


![image](images/W5500-EVB-Pico.png)


## Web UI

![homepage](images/home.png)
Control active scenes, capture them or import export the scene file
![network](images/network.png)
Configure network settings
![sacn](images/sacn.png)
Configure Source name and Streaming ACN settings
![gpio](images/GPIO.png)
Configure GPIO, labeling, and modes<br/>
Toggle =  toggle switch, push on, push off<br/>
Active = Plays a scene while pressed<br/>
Single =  single shot when pressed<br/>
![targets](images/Targets.png)
Configure unicast targets or enable/disable multicast