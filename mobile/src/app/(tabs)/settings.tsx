import { useEffect, useState } from "react";
import { ScrollView } from "react-native";

import { Body, Button, Card, ErrorText, Field, Row, Screen, Title } from "../../components/ui";
import { ApiClient } from "../../lib/api";
import { registerForReplyNotifications } from "../../lib/push";
import { useSettings } from "../../lib/settings";

export default function SettingsScreen() {
  const { backendUrl, token, save, ready } = useSettings();
  const [url, setUrl] = useState(backendUrl);
  const [tok, setTok] = useState(token);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (ready) {
      setUrl(backendUrl);
      setTok(token);
    }
  }, [ready, backendUrl, token]);

  async function connect() {
    setBusy(true);
    setError(null);
    setStatus(null);
    try {
      const cleaned = /^https?:\/\//.test(url.trim()) ? url.trim() : `http://${url.trim()}`;
      const me = await new ApiClient(cleaned.replace(/\/+$/, ""), tok.trim()).me();
      await save(cleaned, tok);
      setStatus(`Connected as ${me.name} (${me.role}).${me.google_connected ? "" : " Google isn't connected on the backend yet."}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function enablePush() {
    setBusy(true);
    setError(null);
    try {
      await registerForReplyNotifications(new ApiClient(backendUrl, token));
      setStatus("You'll get a notification when someone replies.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Screen>
      <ScrollView contentContainerStyle={{ padding: 16 }} keyboardShouldPersistTaps="handled">
        <Card>
          <Title>Backend</Title>
          <Body muted small>
            The address where Press Engine runs, reachable from this phone (for example over Tailscale or a tunnel to D's computer).
          </Body>
          <Field label="Backend address" value={url} onChangeText={setUrl} autoCapitalize="none" autoCorrect={false}
                 keyboardType="url" placeholder="https://press.example.ts.net" />
          <Field label="Your token" value={tok} onChangeText={setTok} autoCapitalize="none" autoCorrect={false}
                 secureTextEntry placeholder="pe_..." />
          <Row><Button title="Save and test" busy={busy} onPress={connect} /></Row>
        </Card>
        <Card>
          <Title>Notifications</Title>
          <Body muted small>Get a push notification when someone replies to a pitch.</Body>
          <Row><Button title="Turn on reply notifications" kind="secondary" disabled={!backendUrl || !token} busy={busy} onPress={enablePush} /></Row>
        </Card>
        <Card>
          <Title>Saving creators</Title>
          <Body muted small>
            In TikTok or Instagram tap Share, then Press Engine (on iOS you may need to tap "More" and add it once). The creator is saved and tagged with your name.
          </Body>
        </Card>
        {status && <Body>{status}</Body>}
        <ErrorText error={error} />
      </ScrollView>
    </Screen>
  );
}
