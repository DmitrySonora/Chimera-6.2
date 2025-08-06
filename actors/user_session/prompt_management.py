from actors.messages import ActorMessage
from config.prompts import PROMPT_CONFIG

class PromptManagementMixin:
    def _should_include_prompt(self, session) -> bool:
        """Определить необходимость включения системного промпта"""
        strategy = PROMPT_CONFIG["prompt_strategy"]
        
        if not PROMPT_CONFIG["enable_periodic_prompt"]:
            return True  # Всегда включать если периодичность отключена
            
        if strategy == "always":
            return True
            
        elif strategy == "periodic":
            # Каждое N-ое сообщение
            interval = PROMPT_CONFIG["system_prompt_interval"]
            return session.message_count % interval == 1
            
        elif strategy == "adaptive":
            # Адаптивная стратегия на основе метрик
            if session.message_count % PROMPT_CONFIG["system_prompt_interval"] == 1:
                return True  # Базовая периодичность
                
            # Проверяем метрики кэша
            if len(session.cache_metrics) >= 5:
                avg_cache_hit = sum(session.cache_metrics[-5:]) / 5
                if avg_cache_hit < PROMPT_CONFIG["cache_hit_threshold"]:
                    # Cache hit rate слишком низкий, включаем промпт
                    return True
                    
        return False
    
    def _get_prompt_reason(self, session) -> str:
        """Получить причину включения промпта для логирования"""
        strategy = PROMPT_CONFIG["prompt_strategy"]
        
        if strategy == "always":
            return "always_strategy"
        elif strategy == "periodic":
            return f"periodic_interval_{PROMPT_CONFIG['system_prompt_interval']}"
        elif strategy == "adaptive":
            if len(session.cache_metrics) >= 5:
                avg_cache_hit = sum(session.cache_metrics[-5:]) / 5
                if avg_cache_hit < PROMPT_CONFIG["cache_hit_threshold"]:
                    return f"low_cache_hit_rate_{avg_cache_hit:.2f}"
            return "adaptive_periodic"
        
        return "unknown"
    
    async def _update_cache_metrics(self, message: ActorMessage) -> None:
        """Обновить метрики кэша для адаптивной стратегии"""
        user_id = message.payload.get('user_id')
        if not user_id or user_id not in self._sessions:
            return
            
        session = self._sessions[user_id]
        cache_hit_rate = message.payload.get('cache_hit_rate', 0.0)
        
        # Сохраняем метрику
        session.cache_metrics.append(cache_hit_rate)
        
        # Ограничиваем размер истории
        if len(session.cache_metrics) > 20:
            session.cache_metrics = session.cache_metrics[-20:]