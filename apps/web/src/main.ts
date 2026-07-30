import { mount } from 'svelte'
import './app.css'
import App from './App.svelte'
import AdStudio from './AdStudio.svelte'

const Component = window.location.pathname === '/test' ? App : AdStudio

const app = mount(Component, {
  target: document.getElementById('app')!,
})

export default app
