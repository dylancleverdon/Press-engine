import type { ReactNode } from "react";
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View, useColorScheme, type TextInputProps } from "react-native";

export function usePalette() {
  const dark = useColorScheme() === "dark";
  return dark
    ? { bg: "#141416", card: "#1d1d20", text: "#ececee", muted: "#9a9aa3", line: "#2e2e33", accent: "#7aa2ff", accentText: "#0d1020", bad: "#ff8a80", ok: "#5fd39a" }
    : { bg: "#f7f7f5", card: "#ffffff", text: "#1d1d1f", muted: "#6b6b72", line: "#e3e3e0", accent: "#2f5fd0", accentText: "#ffffff", bad: "#b3261e", ok: "#1f7a4d" };
}

export function Screen({ children }: { children: ReactNode }) {
  const c = usePalette();
  return <View style={[styles.screen, { backgroundColor: c.bg }]}>{children}</View>;
}

export function Card({ children }: { children: ReactNode }) {
  const c = usePalette();
  return <View style={[styles.card, { backgroundColor: c.card, borderColor: c.line }]}>{children}</View>;
}

export function Title({ children }: { children: ReactNode }) {
  const c = usePalette();
  return <Text style={[styles.title, { color: c.text }]}>{children}</Text>;
}

export function Body({ children, muted, small }: { children: ReactNode; muted?: boolean; small?: boolean }) {
  const c = usePalette();
  return <Text style={{ color: muted ? c.muted : c.text, fontSize: small ? 13 : 15, lineHeight: small ? 18 : 21 }}>{children}</Text>;
}

export function Stat({ value, label }: { value: string | number; label: string }) {
  const c = usePalette();
  return (
    <View style={[styles.stat, { backgroundColor: c.card, borderColor: c.line }]}>
      <Text style={{ color: c.text, fontSize: 22, fontWeight: "700" }}>{value}</Text>
      <Text style={{ color: c.muted, fontSize: 12 }}>{label}</Text>
    </View>
  );
}

export function Button({ title, onPress, kind = "primary", busy, disabled }: {
  title: string;
  onPress: () => void;
  kind?: "primary" | "secondary" | "danger";
  busy?: boolean;
  disabled?: boolean;
}) {
  const c = usePalette();
  const primary = kind === "primary";
  return (
    <Pressable
      accessibilityRole="button"
      onPress={onPress}
      disabled={disabled || busy}
      style={({ pressed }) => [
        styles.button,
        { backgroundColor: primary ? c.accent : c.card, borderColor: primary ? c.accent : c.line, opacity: pressed || disabled ? 0.7 : 1 },
      ]}
    >
      {busy ? (
        <ActivityIndicator color={primary ? c.accentText : c.accent} />
      ) : (
        <Text style={{ color: primary ? c.accentText : kind === "danger" ? c.bad : c.text, fontWeight: "600" }}>{title}</Text>
      )}
    </Pressable>
  );
}

export function Field(props: TextInputProps & { label: string }) {
  const c = usePalette();
  const { label, style, ...rest } = props;
  return (
    <View style={{ marginBottom: 10 }}>
      <Text style={{ color: c.muted, fontSize: 13, marginBottom: 4 }}>{label}</Text>
      <TextInput
        placeholderTextColor={c.muted}
        style={[styles.input, { color: c.text, borderColor: c.line, backgroundColor: c.bg }, rest.multiline ? { minHeight: 140, textAlignVertical: "top" } : null, style]}
        {...rest}
      />
    </View>
  );
}

export function ErrorText({ error }: { error: string | null }) {
  const c = usePalette();
  if (!error) return null;
  return <Text style={{ color: c.bad, marginVertical: 8 }}>{error}</Text>;
}

export function Row({ children }: { children: ReactNode }) {
  return <View style={styles.row}>{children}</View>;
}

const styles = StyleSheet.create({
  screen: { flex: 1 },
  card: { borderWidth: StyleSheet.hairlineWidth, borderRadius: 12, padding: 14, marginBottom: 10 },
  title: { fontSize: 17, fontWeight: "700", marginBottom: 4 },
  stat: { flexGrow: 1, flexBasis: "30%", borderWidth: StyleSheet.hairlineWidth, borderRadius: 12, padding: 12 },
  button: { paddingVertical: 10, paddingHorizontal: 16, borderRadius: 10, borderWidth: 1, alignItems: "center", minWidth: 90 },
  input: { borderWidth: 1, borderRadius: 8, paddingHorizontal: 10, paddingVertical: 8, fontSize: 15 },
  row: { flexDirection: "row", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 8 },
});
