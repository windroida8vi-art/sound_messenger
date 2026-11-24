package com.windroida.audiohide

import android.animation.ObjectAnimator
import android.animation.PropertyValuesHolder
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator

object Ui {
    private val tag = R.id.pulseTag

    fun startPulse(v: View) {
        stopPulse(v)
        v.visibility = View.VISIBLE
        val a = ObjectAnimator.ofPropertyValuesHolder(
            v,
            PropertyValuesHolder.ofFloat(View.SCALE_X, 0.85f, 1.25f),
            PropertyValuesHolder.ofFloat(View.SCALE_Y, 0.85f, 1.25f),
            PropertyValuesHolder.ofFloat(View.ALPHA, 0.9f, 0.25f)
        )
        a.duration = 900
        a.repeatCount = ObjectAnimator.INFINITE
        a.repeatMode = ObjectAnimator.REVERSE
        a.interpolator = AccelerateDecelerateInterpolator()
        v.setTag(tag, a)
        a.start()
    }

    fun stopPulse(v: View) {
        (v.getTag(tag) as? ObjectAnimator)?.cancel()
        v.setTag(tag, null)
        v.scaleX = 1f
        v.scaleY = 1f
        v.alpha = 0.35f
    }
}
