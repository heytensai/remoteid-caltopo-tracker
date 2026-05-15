#!/bin/bash

d=$(date -R)
INT="${1}"
if [ "${INT}" = "" ]
then
	echo "${d} usage: ${0} [wifi interface]"
	exit 1
fi

iwconfig "${INT}" | grep -qi monitor || {
	echo "${d} ${INT} set monitor mode"
	ip link set dev "${INT}" down
	iwconfig "${INT}" mode monitor
	ip link set dev "${INT}" up
}

iwconfig "${INT}" | grep -qi 2.437 || {
	echo "${d} ${INT} set channel 6"
	iw dev "${INT}" set channel 6
}
