#!/bin/bash

INT="${1}"
if [ "${INT}" = "" ]
then
	echo "usage: ${0} [wifi interface]"
	exit 1
fi

ip link set dev "${INT}" down
iwconfig "${INT}" mode monitor
ip link set dev "${INT}" up
iw dev "${INT}" set channel 6
