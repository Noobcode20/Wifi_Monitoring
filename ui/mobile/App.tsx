import { useEffect } from 'react';
import { NavigationContainer, DarkTheme } from '@react-navigation/native';
import { GestureHandlerRootView } from 'react-native-gesture-handler';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { apiService } from '@/services/api.service';
import { ThemeProvider } from './src/theme/ThemeContext';
import { useSettingsStore } from './src/stores/settingsStore';
import { RootNavigator } from './src/navigation/RootNavigator';

export default function App() {
  const serverUrl = useSettingsStore((state) => state.serverUrl);

  useEffect(() => {
    apiService.setBaseUrl(serverUrl);
  }, [serverUrl]);

  useEffect(() => {
    (globalThis as { __appStartTime?: number }).__appStartTime = Date.now();
  }, []);

  const navigationTheme = {
    ...DarkTheme,
    colors: {
      ...DarkTheme.colors,
      background: '#F5F8F6',
      card: '#FFFFFF',
      text: '#17221E',
      border: '#E3EAE6',
      primary: '#08785A',
    },
  };

  return (
    <GestureHandlerRootView style={{ flex: 1 }}>
      <SafeAreaProvider>
        <ThemeProvider>
          <NavigationContainer theme={navigationTheme}>
            <RootNavigator />
          </NavigationContainer>
        </ThemeProvider>
      </SafeAreaProvider>
      <StatusBar style="dark" />
    </GestureHandlerRootView>
  );
}
