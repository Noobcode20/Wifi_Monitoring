import { useEffect, useState } from 'react';
import { ScrollView, StyleSheet, View } from 'react-native';
import { ThemedText } from '@/components/ThemedText';
import { ThemedView } from '@/components/ThemedView';
import { colors } from '@/theme/colors';
import { spacing } from '@/theme/spacing';
import { apiService } from '@/services/api.service';
import { useSettingsStore } from '@/stores/settingsStore';
import { ServerUrlInput } from './ServerUrlInput';

export const SettingsScreen = () => {
  const serverUrl = useSettingsStore((state) => state.serverUrl);
  const setServerUrl = useSettingsStore((state) => state.setServerUrl);
  const [draftUrl, setDraftUrl] = useState(serverUrl);

  useEffect(() => setDraftUrl(serverUrl), [serverUrl]);

  const handleSaveUrl = () => {
    const nextUrl = draftUrl.trim().replace(/\/$/, '');
    setServerUrl(nextUrl);
    apiService.setBaseUrl(nextUrl);
  };

  return (
    <ThemedView style={styles.screen}>
      <ScrollView contentContainerStyle={styles.content}>
        <ThemedText style={styles.kicker}>RUView · PREFERENCES</ThemedText>
        <ThemedText style={styles.title}>Settings</ThemedText>
        <View style={styles.panel}>
          <ThemedText style={styles.section}>Local receiver</ThemedText>
          <ThemedText style={styles.copy}>Connect to the RuView server on the same Wi-Fi network. The mobile app reads live node status and system events from this address.</ThemedText>
          <ServerUrlInput value={draftUrl} onChange={setDraftUrl} onSave={handleSaveUrl} />
        </View>
        <ThemedText style={styles.note}>On a phone, enter your computer’s Wi-Fi IP address and port 3002. For example: http://192.168.1.20:3002</ThemedText>
      </ScrollView>
    </ThemedView>
  );
};

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  content: { padding: spacing.lg, paddingTop: 28, paddingBottom: 36 },
  kicker: { color: colors.accent, fontSize: 10, letterSpacing: 1.6, fontWeight: '700' },
  title: { color: colors.textPrimary, fontSize: 30, fontWeight: '700', marginTop: 5, marginBottom: 18 },
  panel: { backgroundColor: colors.surface, borderRadius: 14, padding: spacing.md, borderWidth: 1, borderColor: colors.border },
  section: { color: colors.textPrimary, fontSize: 16, fontWeight: '700' },
  copy: { color: colors.textSecondary, fontSize: 12, lineHeight: 18, marginTop: 5, marginBottom: spacing.md },
  note: { color: colors.textSecondary, fontSize: 12, lineHeight: 18, marginTop: spacing.md },
});

export default SettingsScreen;
