#!/bin/bash

path=$2
meter=$1
rm -rf "pin_log_$meter.txt"

digit () {
  echo -en '\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00' > "$path"
}

for i in {0..9999}
do
   digit
   sleep 1
   digit
   sleep 2

   echo "trying: $i"
   let "k = $i / 1000"
   echo "$k***"
   for((l=1;l<=$k;++l)) do
      digit
      sleep 1
   done
   sleep 3

   let "h = $i / 100 - $k * 10"
   echo "*$h**"
   for((l=1;l<=$h;++l)) do
      digit
      sleep 1
   done
   sleep 3

   let "d = $i / 10 - $h*10 - $k *100"
   echo "**$d*"
   for((l=1;l<=$d;++l)) do
      digit
      sleep 1
   done
   sleep 3

   let "s = $i - $d*10 - $h*100 - $k*1000"
   echo "***$s"
   for((l=1;l<=$s;++l)) do
      digit
      sleep 1
   done
   sleep 3

   echo "$i" >> "pin_log_$meter.txt"

   echo ""
   sleep 1
   val=""
   while [ -z "$val" ]
   do
      val=$(tail -n 50 /var/log/vzlogger.log | grep "$meter" | grep "/ObisIdentifier:1-0:1.8.0" | tail -n1 | sed 's/.*value=//' | sed 's/.00 ts=.*//')
   done
   echo $val 
   let "y = $val - $val / 1000 * 1000"
   echo $y
   if [ "$y" -gt 0 ]; then
	echo "$i" > pin_$meter.txt
        echo "PIN: $i"
        exit 0
   fi
done
