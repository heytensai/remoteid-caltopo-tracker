#!/bin/bash

d=$(date -R)

INT="${1}"
if [ "${INT}" = "" ]
then
	echo "${d} usage: ${0} [wifi interface]"
	exit 1
fi

FREQ="${2}"
if [ "${FREQ}" = "" ]
then
	FREQ="2.437"
fi

CHAN="${3}"
if [ "${CHAN}" = "" ]
then
	CHAN="6"
fi

/usr/sbin/iwconfig "${INT}" | grep -qi monitor || {
	echo "${d} ${INT} set monitor mode"
	/usr/sbin/ip link set dev "${INT}" down
	/usr/sbin/iwconfig "${INT}" mode monitor
	/usr/sbin/ip link set dev "${INT}" up
}

/usr/sbin/iwconfig "${INT}" | grep -qi "${FREQ}" || {
	echo "${d} ${INT} set channel ${CHAN}"
	/usr/sbin/iw dev "${INT}" set channel ${CHAN}
}
