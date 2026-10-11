import React, { Suspense } from 'react';
import { ActivityIndicator } from 'react-native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { Ionicons } from '@expo/vector-icons';
import { ThemedText } from '../components/ThemedText';
import { ThemedView } from '../components/ThemedView';
import { colors } from '../theme/colors';
import { MainTabsParamList } from './types';

const createPlaceholder = (label: string) => {
  const Placeholder = () => (
    <ThemedView style={{ flex: 1, alignItems: 'center', justifyContent: 'center' }}>
      <ThemedText preset="bodyLg">{label} screen not implemented yet</ThemedText>
      <ThemedText preset="bodySm" color="textSecondary">
        Placeholder shell
      </ThemedText>
    </ThemedView>
  );
  const LazyPlaceholder = React.lazy(async () => ({ default: Placeholder }));

  const Wrapped = () => (
    <Suspense
      fallback={
        <ThemedView style={{ flex: 1, alignItems: 'center', justifyContent: 'center' }}>
          <ActivityIndicator color={colors.accent} />
          <ThemedText preset="bodySm" color="textSecondary" style={{ marginTop: 8 }}>
            Loading {label}
          </ThemedText>
        </ThemedView>
      }
    >
      <LazyPlaceholder />
    </Suspense>
  );

  return Wrapped;
};

const wrapLazy = (
  loader: () => Promise<{ default: React.ComponentType }>,
  label: string,
) => {
  const fallback = createPlaceholder(label);
  return React.lazy(async () => {
    try {
      const module = await loader();
      if (module?.default) {
        return module;
      }
    } catch {
      // keep fallback for shell-only screens
    }
    return { default: fallback } as { default: React.ComponentType };
  });
};

const OverviewScreen = wrapLazy(() => import('../screens/OverviewScreen'), 'Overview');
const ActivityScreen = wrapLazy(() => import('../screens/ActivityScreen'), 'Activity');
const SettingsScreen = wrapLazy(() => import('../screens/SettingsScreen'), 'Settings');

const toIconName = (routeName: keyof MainTabsParamList) => {
  switch (routeName) {
    case 'Overview':
      return 'home';
    case 'Activity':
      return 'notifications';
    case 'Settings':
      return 'settings';
    default:
      return 'ellipse';
  }
};

const screens: ReadonlyArray<{ name: keyof MainTabsParamList; component: React.ComponentType }> = [
  { name: 'Overview', component: OverviewScreen },
  { name: 'Activity', component: ActivityScreen },
  { name: 'Settings', component: SettingsScreen },
];

const Tab = createBottomTabNavigator<MainTabsParamList>();

const Suspended = ({ component: Component }: { component: React.ComponentType }) => (
  <Suspense fallback={<ActivityIndicator color={colors.accent} />}>
    <Component />
  </Suspense>
);

export const MainTabs = () => {
  return (
    <Tab.Navigator
      initialRouteName="Overview"
      screenOptions={({ route }) => ({
        headerShown: false,
        tabBarActiveTintColor: colors.accent,
        tabBarInactiveTintColor: colors.textSecondary,
        tabBarStyle: {
          backgroundColor: '#FFFFFF',
          borderTopColor: '#E3EAE6',
          borderTopWidth: 1,
        },
        tabBarIcon: ({ color, size }) => <Ionicons name={toIconName(route.name)} size={size} color={color} />,
        tabBarLabelStyle: {
          fontFamily: 'Courier New',
          textTransform: 'uppercase',
          fontSize: 10,
        },
      })}
    >
      {screens.map(({ name, component }) => (
        <Tab.Screen
          key={name}
          name={name}
          options={{
            tabBarLabel: name === 'Activity' ? 'Alerts' : name,
          }}
        >
          {() => <Suspended component={component} />}
        </Tab.Screen>
      ))}
    </Tab.Navigator>
  );
};
