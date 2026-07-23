from smlgw.publisher import RecordingPublisher


def test_records_messages_and_latest_per_topic():
    pub = RecordingPublisher()
    pub.connect()
    assert pub.connected is True
    pub.publish("a/b", "1")
    pub.publish("a/b", "2", retain=True)
    pub.publish("c/d", "9")
    assert pub.topics == {"a/b": "2", "c/d": "9"}
    assert pub.payloads_for("a/b") == ["1", "2"]
    assert pub.messages[1] == ("a/b", "2", True)
    pub.disconnect()
    assert pub.connected is False


def test_mqtt_publisher_builds_without_broker():
    # Constructing must not require a broker or a paho connection.
    from smlgw.publisher import MqttPublisher

    pub = MqttPublisher("localhost", 1883, client_id="test")
    assert pub.connected is False
