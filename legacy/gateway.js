console.log('welcome to GW')

var SmartmeterObis = require('smartmeter-obis');

var options_heating = {
    'protocol': "SmlProtocol",
    'transport': "SerialResponseTransport",
    'transportSerialPort': "/dev/ttyUSB0",
    'transportSerialBaudrate': 9600,
    'requestInterval': 0,
    'obisNameLanguage': 'en',
    'obisFallbackMedium': 6,
      'debug': 0,
      'transportStdInMessageTimeout': 1000,
      'protocolSmlIgnoreInvalidCRC': true
};

var options_house = {
    'protocol': "SmlProtocol",
    'transport': "SerialResponseTransport",
    'transportSerialPort': "/dev/ttyUSB1",
    'transportSerialBaudrate': 9600,
    'requestInterval': 0,
    'obisNameLanguage': 'en',
    'obisFallbackMedium': 6,
      'debug': 0,
      'transportStdInMessageTimeout': 1000,
      'protocolSmlIgnoreInvalidCRC': true
};

console.log('connecting to mqtt...')
var mqtt = require('mqtt')
var client = mqtt.connect('mqtt://mosquitto.mundhenk.info:1883')
if (client.connected==true){
  console.log('connected to mqtt')
}

function heatingCallback(err, obisResult) {
  if (err) {
    // handle error
    // if you want to cancel the processing because of this error call smTransport.stop() before returning
    // else processing continues
    console.log('received error!')
    console.log(err)
    return;
  }

 var total = obisResult["1-0:1.8.0*255"].valueToString().split(" ")[0];
 var ht = obisResult["1-0:1.8.1*255"].valueToString().split(" ")[0];
 var nt = obisResult["1-0:1.8.2*255"].valueToString().split(" ")[0];
 var current = obisResult["1-0:16.7.0*255"].valueToString().split(" ")[0]

  console.log('publishing power/heating/current at %d', current)
 client.publish('power/heating/current', current)
  console.log('published')
  console.log('publishing power/heating/ht at %d', ht)
 client.publish('power/heating/ht', ht)
  console.log('published')
  console.log('publishing power/heating/nt at %d', nt)
 client.publish('power/heating/nt', nt)
  console.log('published')
  console.log('publishing power/heating/total at %d', total)
 client.publish('power/heating/total', total)
  console.log('published')
}

function houseCallback(err, obisResult) {
  if (err) {
    // handle error
    // if you want to cancel the processing because of this error call smTransport.stop() before returning
    // else processing continues
    console.log('received error!')
    console.log(err)
    return;
  }

 var total = obisResult["1-0:1.8.0*255"].valueToString().split(" ")[0];
 var current = obisResult["1-0:16.7.0*255"].valueToString().split(" ")[0]

  console.log('publishing power/house/current at %f', current)
 client.publish('power/house/current', current)
  console.log('published')
  console.log('publishing power/house/total at %f', current)
 client.publish('power/house/total', total)
  console.log('published')
}

console.log('initializing...')
var smTransport_heating = SmartmeterObis.init(options_heating, heatingCallback);
var smTransport_house = SmartmeterObis.init(options_house, houseCallback);
console.log('starting...')
smTransport_heating.process();
smTransport_house.process();
console.log('running...')

